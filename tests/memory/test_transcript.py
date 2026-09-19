"""Transcript model + storage tests."""

import pytest

from memory import store as store_mod
from memory import transcript as transcript_mod

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
        "session_id": "test-session-1",
        "role": "user",
        "content": "Hello, how do I fix the auth bug?",
        "timestamp": FIXED_TS,
        "metadata": {"model": "gpt-4"},
    }
    fields.update(overrides)
    return transcript_mod.record_message(conn, **fields)


def test_valid_message_round_trip(conn):
    msg_id = _record(conn)
    assert msg_id == 1
    (msg,) = transcript_mod.search_messages(conn)
    assert msg == transcript_mod.TranscriptMessage(
        id=1,
        session_id="test-session-1",
        role="user",
        content="Hello, how do I fix the auth bug?",
        timestamp=FIXED_TS,
        metadata={"model": "gpt-4"},
    )


def test_invalid_messages_rejected(conn):
    with pytest.raises(ValueError, match="session_id must be"):
        _record(conn, session_id="  ")
    with pytest.raises(ValueError, match="unknown role"):
        _record(conn, role="invalid")
    with pytest.raises(ValueError, match="content must be a string"):
        _record(conn, content=123)
    with pytest.raises(ValueError, match="metadata must be a dict"):
        _record(conn, metadata=["not", "a", "dict"])
    with pytest.raises(ValueError, match="not JSON-serializable"):
        _record(conn, metadata={"bad": object()})
    assert transcript_mod.search_messages(conn) == []


def test_role_filter_validated_on_search(conn):
    _record(conn)
    with pytest.raises(ValueError, match="unknown role filter"):
        transcript_mod.search_messages(conn, role="nope")


def test_multiple_messages_distinct_and_ordered(conn):
    _record(conn, role="user", content="first")
    _record(conn, role="assistant", content="second")
    _record(conn, role="user", content="third")
    rows = transcript_mod.search_messages(conn)
    assert [m.id for m in rows] == [1, 2, 3]
    assert [m.role for m in rows] == ["user", "assistant", "user"]
    assert transcript_mod.search_messages(conn, role="assistant")[0].content == "second"


def test_search_filters(conn):
    _record(conn, session_id="session-a", role="user", content="auth bug")
    _record(conn, session_id="session-b", role="assistant", content="fix middleware")
    _record(conn, session_id="session-a", role="assistant", content="reorder middleware")
    assert len(transcript_mod.search_messages(conn, session_id="session-a")) == 2
    assert len(transcript_mod.search_messages(conn, role="assistant")) == 2
    assert len(transcript_mod.search_messages(conn, query="middleware")) == 2
    assert len(transcript_mod.search_messages(conn, session_id="session-a", query="middleware")) == 1
    assert transcript_mod.search_messages(conn, session_id="missing") == []
    assert transcript_mod.search_messages(conn, query='"unbalanced') == []


def test_search_bounded(conn):
    for index in range(5):
        _record(conn, content=f"message {index}")
    assert len(transcript_mod.search_messages(conn, limit=2)) == 2
    assert len(transcript_mod.search_messages(conn, limit=9999)) == 5


def test_persistence_across_reopen(tmp_path):
    db_path = tmp_path / "memory.db"
    handle = store_mod.connect(db_path)
    store_mod.init_schema(handle)
    _record(handle)
    handle.close()
    handle = store_mod.connect(db_path)
    try:
        rows = transcript_mod.search_messages(handle)
        assert len(rows) == 1
        assert rows[0].content == "Hello, how do I fix the auth bug?"
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
        _record(handles[0], session_id="s1", content="only in A")
        assert len(transcript_mod.search_messages(handles[0])) == 1
        assert transcript_mod.search_messages(handles[1]) == []
    finally:
        for handle in handles:
            handle.close()


def test_hostile_strings_are_data(conn):
    hostile = "' OR '1'='1'; DROP TABLE transcript_messages; --"
    _record(conn, content=hostile, session_id=hostile)
    assert len(transcript_mod.search_messages(conn, session_id=hostile)) == 1
    assert len(transcript_mod.search_messages(conn)) == 1  # table intact
    assert isinstance(transcript_mod.search_messages(conn, query=hostile), list)


def test_get_session_messages(conn):
    _record(conn, session_id="sess-1", role="user", content="msg 1")
    _record(conn, session_id="sess-1", role="assistant", content="msg 2")
    _record(conn, session_id="sess-2", role="user", content="msg 3")
    msgs = transcript_mod.get_session_messages(conn, "sess-1")
    assert len(msgs) == 2
    assert [m.content for m in msgs] == ["msg 1", "msg 2"]
    assert transcript_mod.get_session_messages(conn, "missing") == []


def test_list_sessions(conn):
    _record(conn, session_id="sess-1", role="user", content="msg 1", timestamp="2026-03-01T10:00:00+00:00")
    _record(conn, session_id="sess-1", role="assistant", content="msg 2", timestamp="2026-03-01T10:00:01+00:00")
    _record(conn, session_id="sess-2", role="user", content="msg 3", timestamp="2026-03-01T10:00:02+00:00")
    sessions = transcript_mod.list_sessions(conn)
    assert len(sessions) == 2
    # Newest session first (sess-2 has the most recent message)
    assert sessions[0]["session_id"] == "sess-2"
    assert sessions[0]["count"] == 1
    assert sessions[1]["session_id"] == "sess-1"
    assert sessions[1]["count"] == 2


def test_count_messages(conn):
    assert transcript_mod.count_messages(conn) == 0
    _record(conn)
    assert transcript_mod.count_messages(conn) == 1
    _record(conn)
    assert transcript_mod.count_messages(conn) == 2