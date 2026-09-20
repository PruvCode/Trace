"""SQLite + FTS5 storage for structural memory (workspace-local file).

Schema:
  symbols(name, kind, file, line, signature)
  relationships(src, rel, dst, file, line)   # rel in {"calls", "imports"}
  symbols_fts: FTS5 index over (name, kind, file, signature), rowid = symbols.id
  events(type, timestamp, source, repo, commit?, file?, symbol?, payload JSON)
  events_fts: FTS5 index over (type, symbol, file, payload_text), rowid = events.id
  transcript_messages(id, session_id, role, content, timestamp, metadata JSON)
  transcript_messages_fts: FTS5 index over (session_id, role, content), rowid = transcript_messages.id

All queries are parameterized. All results are bounded (default 10, max 50).
FTS5 is an implementation detail of this backend; callers only see dicts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_LIMIT = 10
MAX_LIMIT = 50

_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS transcript_messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_transcript_session ON transcript_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_transcript_timestamp ON transcript_messages(timestamp);
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_messages_fts USING fts5(
    session_id, role, content
);
CREATE TABLE IF NOT EXISTS structural_fingerprint (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    git_tree_hash TEXT,
    file_mtimes TEXT,
    updated_at TEXT NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the workspace-local database (parents created on demand)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def clear(conn: sqlite3.Connection) -> None:
    """Wipe structural rows so re-indexing is deterministic (events kept)."""
    conn.execute("DELETE FROM symbols")
    conn.execute("DELETE FROM relationships")
    conn.execute("DELETE FROM symbols_fts")
    conn.commit()


def clear_events(conn: sqlite3.Connection) -> None:
    """Wipe episodic rows (explicit only; re-indexing never clears events)."""
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM events_fts")
    conn.commit()


def insert_symbol(
    conn: sqlite3.Connection,
    name: str,
    kind: str,
    file: str,
    line: int,
    signature: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO symbols (name, kind, file, line, signature)"
        " VALUES (?, ?, ?, ?, ?)",
        (name, kind, file, line, signature),
    )
    return cursor.lastrowid


def insert_relationship(
    conn: sqlite3.Connection,
    src: str,
    rel: str,
    dst: str,
    file: str,
    line: int,
) -> None:
    conn.execute(
        "INSERT INTO relationships (src, rel, dst, file, line)"
        " VALUES (?, ?, ?, ?, ?)",
        (src, rel, dst, file, line),
    )


def rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM symbols_fts")
    conn.execute(
        "INSERT INTO symbols_fts (rowid, name, kind, file, signature)"
        " SELECT id, name, kind, file, signature FROM symbols"
    )
    conn.commit()


def clamp_limit(limit: object) -> int:
    try:
        value = int(limit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def get_structural_fingerprint(conn: sqlite3.Connection) -> dict | None:
    """Get the stored structural fingerprint."""
    row = conn.execute(
        "SELECT git_tree_hash, file_mtimes, updated_at FROM structural_fingerprint WHERE id = 1"
    ).fetchone()
    if row:
        return {"git_tree_hash": row[0], "file_mtimes": row[1], "updated_at": row[2]}
    return None


def set_structural_fingerprint(
    conn: sqlite3.Connection,
    git_tree_hash: str | None,
    file_mtimes: str | None,
) -> None:
    """Store the structural fingerprint."""
    import datetime
    updated_at = datetime.datetime.utcnow().isoformat() + "Z"
    conn.execute(
        "INSERT OR REPLACE INTO structural_fingerprint (id, git_tree_hash, file_mtimes, updated_at) "
        "VALUES (1, ?, ?, ?)",
        (git_tree_hash, file_mtimes, updated_at),
    )
    conn.commit()


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]


def find_definition(conn: sqlite3.Connection, name: str, limit: int = 10) -> list[dict]:
    """Exact-name definitions, ordered deterministically by file then line."""
    if not name or not name.strip():
        return []
    rows = conn.execute(
        "SELECT name, kind, file, line, signature FROM symbols"
        " WHERE name = ? ORDER BY file, line LIMIT ?",
        (name.strip(), clamp_limit(limit)),
    ).fetchall()
    return _rows_to_dicts(rows)


def find_callers(conn: sqlite3.Connection, name: str, limit: int = 10) -> list[dict]:
    """Call sites of `name`: caller function, file, and line (bounded)."""
    if not name or not name.strip():
        return []
    rows = conn.execute(
        "SELECT src AS caller, file, line FROM relationships"
        " WHERE rel = 'calls' AND dst = ? ORDER BY file, line LIMIT ?",
        (name.strip(), clamp_limit(limit)),
    ).fetchall()
    return _rows_to_dicts(rows)


def search_symbols(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    """Lexical search over name/kind/file/signature. Narrow excerpts only.

    Malformed FTS5 queries (unbalanced quotes, bare operators) return [] by
    design instead of raising: retrieval quality is measured downstream by
    task outcomes, never by query cleverness.
    """
    if not query or not query.strip():
        return []
    try:
        rows = conn.execute(
            "SELECT s.name, s.kind, s.file, s.line, s.signature"
            " FROM symbols_fts f JOIN symbols s ON s.id = f.rowid"
            " WHERE symbols_fts MATCH ? LIMIT ?",
            (query.strip(), clamp_limit(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return _rows_to_dicts(rows)
