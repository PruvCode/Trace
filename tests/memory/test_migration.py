"""Migration safety tests: old databases must survive schema additions."""

import sqlite3
import tempfile
from pathlib import Path

from memory import store as store_mod
from memory import episodic as episodic_mod
from memory import transcript as transcript_mod
from memory import structural as structural_mod


FIXED_TS = "2026-03-01T10:00:00+00:00"


def _create_old_schema_db(db_path: Path):
    """Create a database with the OLD schema (no transcript tables)."""
    conn = store_mod.connect(db_path)
    # Old schema from before transcript was added
    old_schema = """
    CREATE TABLE IF NOT EXISTS symbols (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        file TEXT NOT NULL,
        line INTEGER NOT NULL,
        signature TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS relationships (
        id INTEGER PRIMARY KEY,
        src TEXT NOT NULL,
        rel TEXT NOT NULL,
        dst TEXT NOT NULL,
        file TEXT NOT NULL,
        line INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
    CREATE INDEX IF NOT EXISTS idx_rel_dst ON relationships(rel, dst);
    CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
        name, kind, file, signature
    );
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY,
        type TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        source TEXT NOT NULL,
        repo TEXT NOT NULL,
        commit_sha TEXT,
        file TEXT,
        symbol TEXT,
        payload TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
    CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol);
    CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
        type, symbol, file, payload_text
    );
    """
    conn.executescript(old_schema)
    conn.commit()
    return conn


def _seed_old_data(conn: sqlite3.Connection):
    """Seed structural + episodic data using old APIs."""
    # Add some structural data
    store_mod.insert_symbol(conn, "get_session_timeout", "function", "auth/tokens.py", 10, "def get_session_timeout():")
    store_mod.insert_symbol(conn, "login", "function", "auth/login.py", 20, "def login():")
    store_mod.insert_relationship(conn, "login", "calls", "get_session_timeout", "auth/login.py", 25)
    store_mod.rebuild_fts(conn)

    # Add some episodic data
    episodic_mod.record_event(
        conn,
        type="investigation",
        source="agent",
        timestamp=FIXED_TS,
        repo="test-repo",
        symbol="get_session_timeout",
        payload={"finding": "timeout is 30s"},
    )
    episodic_mod.record_event(
        conn,
        type="decision",
        source="agent",
        timestamp=FIXED_TS,
        repo="test-repo",
        symbol="login",
        payload={"decision": "keep timeout at 30"},
    )
    conn.commit()


def test_old_database_opens_and_works(tmp_path):
    """An old database (no transcript tables) must open and work after init_schema."""
    db_path = tmp_path / "memory.db"
    conn = _create_old_schema_db(db_path)
    _seed_old_data(conn)
    conn.close()

    # Re-open and run init_schema (migration)
    conn = store_mod.connect(db_path)
    store_mod.init_schema(conn)  # This should add transcript tables additively
    conn.close()

    # Verify structural data still works
    conn = store_mod.connect(db_path)
    defs = store_mod.find_definition(conn, "get_session_timeout")
    assert len(defs) == 1
    assert defs[0]["name"] == "get_session_timeout"

    callers = store_mod.find_callers(conn, "get_session_timeout")
    assert len(callers) == 1
    assert callers[0]["caller"] == "login"

    # Verify episodic data still works
    events = episodic_mod.search_events(conn)
    assert len(events) == 2
    assert events[0].payload == {"finding": "timeout is 30s"}
    assert events[1].payload == {"decision": "keep timeout at 30"}

    # Verify transcript tables exist and work
    msgs = transcript_mod.search_messages(conn)
    assert msgs == []  # empty but functional

    # Record a transcript message
    msg_id = transcript_mod.record_message(
        conn,
        session_id="new-session",
        role="user",
        content="test transcript",
    )
    assert msg_id == 1
    msgs = transcript_mod.search_messages(conn)
    assert len(msgs) == 1
    assert msgs[0].content == "test transcript"

    conn.close()


def test_repeated_init_schema_is_safe(tmp_path):
    """Running init_schema multiple times must not corrupt data."""
    db_path = tmp_path / "memory.db"
    conn = store_mod.connect(db_path)
    store_mod.init_schema(conn)

    # Add some data
    transcript_mod.record_message(conn, session_id="s1", role="user", content="msg1")
    conn.close()

    # Run init_schema again
    conn = store_mod.connect(db_path)
    store_mod.init_schema(conn)
    conn.close()

    # Data should still be there
    conn = store_mod.connect(db_path)
    msgs = transcript_mod.search_messages(conn)
    assert len(msgs) == 1
    assert msgs[0].content == "msg1"
    conn.close()


def test_index_workspace_preserves_transcripts(tmp_path):
    """structural.index_workspace must not delete transcript messages."""
    # Create project with structural + transcript data
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def foo():\n    return 1\n", encoding="utf-8")

    db_path = project / ".agent-memory" / "memory.db"
    structural_mod.index_workspace(project, db_path)

    conn = store_mod.connect(db_path)
    transcript_mod.record_message(conn, session_id="s1", role="user", content="before reindex")
    conn.close()

    # Re-index (this calls clear() which should NOT affect transcript tables)
    structural_mod.index_workspace(project, db_path)

    conn = store_mod.connect(db_path)
    msgs = transcript_mod.search_messages(conn)
    assert len(msgs) == 1
    assert msgs[0].content == "before reindex"

    # Structural data should be rebuilt
    defs = store_mod.find_definition(conn, "foo")
    assert len(defs) == 1
    conn.close()


def test_clear_events_does_not_affect_transcripts(tmp_path):
    """episodic clear_events must not delete transcript messages."""
    db_path = tmp_path / "memory.db"
    conn = store_mod.connect(db_path)
    store_mod.init_schema(conn)

    # Add episodic event
    episodic_mod.record_event(
        conn,
        type="investigation",
        source="agent",
        timestamp=FIXED_TS,
        repo="test-repo",
        payload={"finding": "test"},
    )

    # Add transcript message
    transcript_mod.record_message(conn, session_id="s1", role="user", content="transcript msg")

    # Clear episodic events
    store_mod.clear_events(conn)

    # Episodic should be gone
    events = episodic_mod.search_events(conn)
    assert events == []

    # Transcript should remain
    msgs = transcript_mod.search_messages(conn)
    assert len(msgs) == 1
    assert msgs[0].content == "transcript msg"
    conn.close()