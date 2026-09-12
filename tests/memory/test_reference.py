"""ReferenceBackend lifecycle tests: setup/index/preseed/teardown (Phase 4.2)."""

from pathlib import Path

from memory import episodic as episodic_mod
from memory import store as store_mod
from memory.interface import MCPConfig
from memory.reference import ReferenceBackend, workspace_db_path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXPECTED_TOOLS = {
    "find_definition",
    "find_callers",
    "search_symbols",
    "record_event",
    "search_events",
    "get_git_history",
}


def _workspace(tmp_path, seeded_fixture, name="ws"):
    from benchmark import workspace as workspace_mod

    fixture_dir, head = seeded_fixture
    return workspace_mod.create_workspace(fixture_dir, head, tmp_path / name)


def test_setup_indexes_and_starts(tmp_path, seeded_fixture):
    ws = _workspace(tmp_path, seeded_fixture)
    backend = ReferenceBackend(repo_root=REPO_ROOT)
    try:
        config = backend.setup(ws)
        assert isinstance(config, MCPConfig)
        assert set(config.tools) == EXPECTED_TOOLS
        assert {t.name for t in backend.tool_definitions()} == EXPECTED_TOOLS
        db = workspace_db_path(ws)
        assert db.exists()
        conn = store_mod.connect(db)
        try:
            assert store_mod.find_definition(conn, "refresh_token") != []
        finally:
            conn.close()
    finally:
        backend.teardown()
    assert backend.tool_definitions() == []


def test_teardown_leaves_no_session(tmp_path, seeded_fixture):
    ws = _workspace(tmp_path, seeded_fixture)
    backend = ReferenceBackend(repo_root=REPO_ROOT)
    try:
        backend.setup(ws)
        assert backend._bridge is not None and backend._bridge.alive()
    finally:
        backend.teardown()
    assert backend._bridge is None


def test_preseed_applied_and_validated(tmp_path, seeded_fixture):
    ws = _workspace(tmp_path, seeded_fixture)
    backend = ReferenceBackend(
        repo_root=REPO_ROOT,
        preseed=[
            {
                "type": "observation",
                "source": "agent",
                "timestamp": "2026-04-01T00:00:00+00:00",
                "repo": "preseed-test",
                "symbol": "refresh_token",
                "payload": {"finding": "preseeded"},
            }
        ],
    )
    try:
        backend.setup(ws)
        conn = store_mod.connect(workspace_db_path(ws))
        try:
            rows = episodic_mod.search_events(conn, query="preseeded")
            assert len(rows) == 1 and rows[0].symbol == "refresh_token"
        finally:
            conn.close()
    finally:
        backend.teardown()


def test_bad_preseed_fails_loudly(tmp_path, seeded_fixture):
    ws = _workspace(tmp_path, seeded_fixture)
    backend = ReferenceBackend(repo_root=REPO_ROOT, preseed=[{"type": "bogus"}])
    try:
        import pytest

        with pytest.raises(ValueError, match="invalid preseed event"):
            backend.setup(ws)
    finally:
        backend.teardown()
    backend = ReferenceBackend(
        repo_root=REPO_ROOT,
        preseed=[{"type": "bogus", "repo": "r", "payload": {}}],
    )
    try:
        import pytest

        with pytest.raises(ValueError, match="unknown event type"):
            backend.setup(ws)
    finally:
        backend.teardown()


def test_workspaces_isolated(tmp_path, seeded_fixture):
    ws_a = _workspace(tmp_path, seeded_fixture, "ws_a")
    ws_b = _workspace(tmp_path, seeded_fixture, "ws_b")
    backend_a = ReferenceBackend(repo_root=REPO_ROOT)
    backend_b = ReferenceBackend(repo_root=REPO_ROOT)
    try:
        backend_a.setup(ws_a)
        backend_b.setup(ws_b)
        assert workspace_db_path(ws_a) != workspace_db_path(ws_b)
        conn = store_mod.connect(workspace_db_path(ws_a))
        try:
            episodic_mod.record_event(
                conn,
                type="observation",
                timestamp="2026-04-01T00:00:00+00:00",
                repo="ws_a",
                payload={"finding": "only in A"},
            )
        finally:
            conn.close()
        conn_b = store_mod.connect(workspace_db_path(ws_b))
        try:
            assert episodic_mod.search_events(conn_b) == []
        finally:
            conn_b.close()
    finally:
        backend_a.teardown()
        backend_b.teardown()


def test_reset_removes_db(tmp_path, seeded_fixture):
    ws = _workspace(tmp_path, seeded_fixture)
    backend = ReferenceBackend(repo_root=REPO_ROOT)
    try:
        backend.setup(ws)
        assert workspace_db_path(ws).exists()
    finally:
        backend.reset()
    assert not workspace_db_path(ws).exists()
