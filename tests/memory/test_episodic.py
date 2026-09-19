"""Episodic model + storage tests (Phase 3.1)."""

import pytest

from memory import episodic as episodic_mod
from memory import store as store_mod

FIXED_TS = "2026-03-01T10:00:00+00:00"


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / ".agent-memory" / "memory.db"
    handle = store_mod.connect(db_path)
    store_mod.init_schema(handle)
    yield handle
    handle.close()


def _record(conn, **overrides):
    fields = {
        "type": "investigation",
        "source": "agent",
        "timestamp": FIXED_TS,
        "repo": "test-repo",
        "symbol": "refresh_token",
        "payload": {"finding": "refresh_token() is never called"},
    }
    fields.update(overrides)
    return episodic_mod.record_event(conn, **fields)


def test_valid_event_round_trip(conn):
    event_id = _record(conn)
    assert event_id == 1
    (event,) = episodic_mod.search_events(conn)
    assert event == episodic_mod.Event(
        id=1,
        type="investigation",
        timestamp=FIXED_TS,
        source="agent",
        repo="test-repo",
        commit=None,
        file=None,
        symbol="refresh_token",
        payload={"finding": "refresh_token() is never called"},
    )


def test_invalid_events_rejected(conn):
    with pytest.raises(ValueError, match="unknown event type"):
        _record(conn, type="hallucination")
    with pytest.raises(ValueError, match="unknown event source"):
        _record(conn, source="llm")
    with pytest.raises(ValueError, match="repo must be"):
        _record(conn, repo="  ")
    with pytest.raises(ValueError, match="payload must be a dict"):
        _record(conn, payload=["not", "a", "dict"])
    with pytest.raises(ValueError, match="not JSON-serializable"):
        _record(conn, payload={"bad": object()})
    with pytest.raises(ValueError, match="symbol must be"):
        _record(conn, symbol="  ")
    assert episodic_mod.search_events(conn) == []


def test_event_types_validated_on_search_filter(conn):
    _record(conn)
    with pytest.raises(ValueError, match="unknown event type filter"):
        episodic_mod.search_events(conn, type="nope")


def test_multiple_events_distinct_and_ordered(conn):
    _record(conn, type="investigation", payload={"finding": "first"})
    _record(conn, type="attempt", payload={"action": "second", "result": "failed"})
    _record(conn, type="decision", payload={"decision": "third"})
    rows = episodic_mod.search_events(conn)
    assert [e.id for e in rows] == [1, 2, 3]
    assert [e.type for e in rows] == ["investigation", "attempt", "decision"]
    assert episodic_mod.search_events(conn, type="attempt")[0].payload == {
        "action": "second",
        "result": "failed",
    }


def test_search_filters(conn):
    _record(conn, symbol="refresh_token", file="auth/tokens.py")
    _record(
        conn,
        type="attempt",
        symbol="validate_session",
        file="auth/session.py",
        commit="abc123",
        payload={"action": "raised timeout", "result": "no effect"},
    )
    assert len(episodic_mod.search_events(conn, symbol="refresh_token")) == 1
    assert len(episodic_mod.search_events(conn, file="auth/session.py")) == 1
    assert len(episodic_mod.search_events(conn, commit="abc123")) == 1
    assert len(episodic_mod.search_events(conn, query="timeout")) == 1
    assert episodic_mod.search_events(conn, symbol="missing") == []
    assert episodic_mod.search_events(conn, query='"unbalanced') == []


def test_search_bounded(conn):
    for index in range(5):
        _record(conn, payload={"finding": f"finding {index}"})
    assert len(episodic_mod.search_events(conn, limit=2)) == 2
    assert len(episodic_mod.search_events(conn, limit=9999)) == 5


def test_persistence_across_reopen(tmp_path):
    db_path = tmp_path / "memory.db"
    handle = store_mod.connect(db_path)
    store_mod.init_schema(handle)
    _record(handle)
    handle.close()
    handle = store_mod.connect(db_path)
    try:
        rows = episodic_mod.search_events(handle)
        assert len(rows) == 1
        assert rows[0].payload == {"finding": "refresh_token() is never called"}
    finally:
        handle.close()


def test_workspace_dbs_isolated(tmp_path):
    paths = [tmp_path / "a.db", tmp_path / "b.db"]
    handles = []
    for path in paths:
        handle = store_mod.connect(path)
        store_mod.init_schema(handle)
        handles.append(handle)
    try:
        _record(handles[0], payload={"finding": "only in A"})
        assert len(episodic_mod.search_events(handles[0])) == 1
        assert episodic_mod.search_events(handles[1]) == []
    finally:
        for handle in handles:
            handle.close()


def test_hostile_strings_are_data(conn):
    hostile = "' OR '1'='1'; DROP TABLE events; --"
    _record(conn, symbol=hostile, payload={"finding": hostile})
    assert len(episodic_mod.search_events(conn, symbol=hostile)) == 1
    assert len(episodic_mod.search_events(conn)) == 1  # table intact
    assert isinstance(episodic_mod.search_events(conn, query=hostile), list)


def test_agent_and_git_sources_distinguishable(conn):
    _record(conn, source="agent", payload={"finding": "agent claim"})
    _record(
        conn,
        type="git_change",
        source="git",
        commit="abc123",
        file="auth/session.py",
        symbol="refresh_token",
        payload={"commit": "abc123", "files": ["auth/session.py"]},
    )
    rows = episodic_mod.search_events(conn)
    assert [e.source for e in rows] == ["agent", "git"]
    assert rows[0].payload == {"finding": "agent claim"}
    assert rows[1].payload == {"commit": "abc123", "files": ["auth/session.py"]}
