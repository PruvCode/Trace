"""Cross-layer integration: git facts persist as source=git events (Phase 3.4).

Proves the intended Phase 5 data path: derive from Git, store with
source="git", retrieve narrowly — while agent claims about the same symbol
stay distinguishable.
"""

from memory import episodic as episodic_mod
from memory import git_events as git_mod
from memory import store as store_mod
from memory import structural as structural_mod

FIXED_TS = "2026-03-02T10:00:00+00:00"


def test_git_facts_persist_and_retrieve(tmp_path, seeded_history):
    history_dir, shas = seeded_history
    db_path = tmp_path / "memory.db"
    conn = store_mod.connect(db_path)
    store_mod.init_schema(conn)
    try:
        c2 = next(
            e for e in git_mod.get_git_history(history_dir) if e["sha"] == shas[1]
        )
        event_id = episodic_mod.record_event(
            conn,
            type="git_change",
            source="git",
            timestamp=FIXED_TS,
            repo="history_repo",
            commit=c2["sha"],
            file="auth/tokens.py",
            symbol="refresh_token",
            payload={"files": [f["path"] for f in c2["files"]]},
        )
        assert event_id == 1
        episodic_mod.record_event(
            conn,
            type="attempt",
            source="agent",
            timestamp=FIXED_TS,
            repo="history_repo",
            symbol="refresh_token",
            payload={"action": "bumped timeout", "result": "no effect"},
        )
        rows = episodic_mod.search_events(
            conn, symbol="refresh_token", commit=c2["sha"]
        )
        assert len(rows) == 1
        assert rows[0].source == "git"
        assert rows[0].payload == {
            "files": ["auth/login.py", "auth/tokens.py"]
        }
        both = episodic_mod.search_events(conn, symbol="refresh_token")
        assert [e.source for e in both] == ["git", "agent"]
    finally:
        conn.close()


def test_structural_and_episodic_share_db(tmp_path, seeded_fixture):
    """Proves table coexistence: indexing never touches events and vice versa."""
    fixture_dir, _head = seeded_fixture
    db_path = tmp_path / "memory.db"
    conn = store_mod.connect(db_path)
    store_mod.init_schema(conn)
    try:
        episodic_mod.record_event(
            conn,
            type="observation",
            timestamp=FIXED_TS,
            repo="toy_repo",
            payload={"finding": "recorded before indexing"},
        )
        stats = structural_mod.index_workspace(fixture_dir, db_path)
        assert stats["symbols"] > 0
        # Fresh connection: structural index committed, event intact.
        conn2 = store_mod.connect(db_path)
        try:
            assert len(episodic_mod.search_events(conn2)) == 1
            assert store_mod.find_callers(conn2, "refresh_token") != []
        finally:
            conn2.close()
    finally:
        conn.close()
