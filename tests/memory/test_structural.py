"""Structural memory tests: definitions, callers, search, bounds, persistence."""

import sqlite3

import pytest

from memory import store as store_mod
from memory import structural as structural_mod


@pytest.fixture()
def indexed_db(tmp_path, seeded_fixture):
    """Index the seeded toy fixture into a workspace-local-style DB."""
    fixture_dir, _head = seeded_fixture
    db_path = tmp_path / ".agent-memory" / "memory.db"
    stats = structural_mod.index_workspace(fixture_dir, db_path)
    assert stats["files"] == 8
    conn = store_mod.connect(db_path)
    yield conn, db_path
    conn.close()


def _dump(conn):
    symbols = conn.execute(
        "SELECT name, kind, file, line, signature FROM symbols"
        " ORDER BY file, line, name"
    ).fetchall()
    rels = conn.execute(
        "SELECT src, rel, dst, file, line FROM relationships"
        " ORDER BY file, line, src, dst"
    ).fetchall()
    return [tuple(r) for r in symbols], [tuple(r) for r in rels]


def test_known_definition(indexed_db):
    """Proves definitions expose name/kind/file/line/signature."""
    conn, _db = indexed_db
    rows = store_mod.find_definition(conn, "refresh_token")
    assert rows == [
        {
            "name": "refresh_token",
            "kind": "function",
            "file": "auth/tokens.py",
            "line": 4,
            "signature": "def refresh_token(user_id):",
        }
    ]


def test_cross_file_callers(indexed_db):
    """Acceptance: find_callers(refresh_token) -> both callers, narrow rows."""
    conn, _db = indexed_db
    rows = store_mod.find_callers(conn, "refresh_token")
    assert {(r["caller"], r["file"]) for r in rows} == {
        ("login_and_refresh", "auth/login.py"),
        ("renew_session", "services/renewal.py"),
    }
    assert set(rows[0]) == {"caller", "file", "line"}  # no source dumps


def test_symbol_search(indexed_db):
    conn, _db = indexed_db
    rows = store_mod.search_symbols(conn, "token")
    names = {r["name"] for r in rows}
    assert {"refresh_token", "validate_token"} <= names
    assert len(rows) <= store_mod.DEFAULT_LIMIT
    assert all(set(r) == {"name", "kind", "file", "line", "signature"} for r in rows)


def test_search_bounded(indexed_db):
    conn, _db = indexed_db
    assert len(store_mod.search_symbols(conn, "a", limit=1)) <= 1
    assert store_mod.clamp_limit(9999) == store_mod.MAX_LIMIT
    assert store_mod.clamp_limit("junk") == store_mod.DEFAULT_LIMIT


def test_unknown_and_malformed_queries(indexed_db):
    conn, _db = indexed_db
    assert store_mod.find_definition(conn, "no_such_symbol") == []
    assert store_mod.find_callers(conn, "no_such_symbol") == []
    assert store_mod.find_definition(conn, "   ") == []
    assert store_mod.search_symbols(conn, "") == []
    assert store_mod.search_symbols(conn, '"unbalanced') == []
    assert store_mod.search_symbols(conn, "OR") == []


def test_import_edges_recorded(indexed_db):
    conn, _db = indexed_db
    rows = conn.execute(
        "SELECT src, dst FROM relationships WHERE rel = 'imports'"
    ).fetchall()
    pairs = {(r[0], r[1]) for r in rows}
    assert ("auth/login.py", "auth.tokens") in pairs


def test_class_and_module_level_call(tmp_path):
    """Covers the class branch and the <module> caller branch."""
    root = tmp_path / "mini"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "things.py").write_text(
        "class Cache:\n"
        "    def get(self, key):\n"
        "        return key\n"
        "\n"
        "result = get('x')\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "mini.db"
    structural_mod.index_workspace(root, db_path)
    conn = store_mod.connect(db_path)
    try:
        kinds = {
            r["name"]: r["kind"]
            for r in store_mod.search_symbols(conn, "Cache OR get")
            if r["name"] in ("Cache", "get")
        }
        assert kinds.get("Cache") == "class"
        assert kinds.get("get") == "function"
        callers = store_mod.find_callers(conn, "get")
        assert any(c["caller"] == "<module>" for c in callers)
    finally:
        conn.close()


def test_persistence_and_deterministic_reindex(tmp_path, seeded_fixture):
    """Proves rows survive close/reopen and reindexing is deterministic."""
    fixture_dir, _head = seeded_fixture
    db_path = tmp_path / "memory.db"
    structural_mod.index_workspace(fixture_dir, db_path)
    conn = store_mod.connect(db_path)
    first = _dump(conn)
    conn.close()

    conn = store_mod.connect(db_path)
    assert _dump(conn) == first
    conn.close()

    structural_mod.index_workspace(fixture_dir, db_path)
    conn = store_mod.connect(db_path)
    try:
        assert _dump(conn) == first
    finally:
        conn.close()


def test_workspace_local_dbs_do_not_leak(tmp_path, seeded_fixture):
    """Proves no shared state: different roots -> independent databases."""
    fixture_dir, _head = seeded_fixture
    other = tmp_path / "other"
    other.mkdir(exist_ok=True)
    (other / "solo.py").write_text(
        "def lonely():\n    return 1\n", encoding="utf-8"
    )
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    structural_mod.index_workspace(fixture_dir, db_a)
    structural_mod.index_workspace(other, db_b)
    conn_a = store_mod.connect(db_a)
    conn_b = store_mod.connect(db_b)
    try:
        assert store_mod.find_definition(conn_b, "refresh_token") == []
        assert store_mod.find_definition(conn_a, "lonely") == []
        assert store_mod.find_definition(conn_b, "lonely") != []
    finally:
        conn_a.close()
        conn_b.close()


def test_queries_parameterized_not_interpolated(tmp_path):
    """A hostile name must not break SQL or return rows (no injection)."""
    db_path = tmp_path / "inj.db"
    conn = store_mod.connect(db_path)
    try:
        store_mod.init_schema(conn)
        store_mod.insert_symbol(conn, "harmless", "function", "f.py", 1, "def harmless():")
        conn.commit()
        hostile = "' OR '1'='1"
        assert store_mod.find_definition(conn, hostile) == []
        assert store_mod.find_callers(conn, hostile) == []
        assert isinstance(store_mod.search_symbols(conn, hostile), list)
        assert sqlite3 is not None  # keeps the import meaningful
    finally:
        conn.close()
