"""SQLite + FTS5 storage for structural memory (workspace-local file).

Schema:
  symbols(name, kind, file, line, signature)
  relationships(src, rel, dst, file, line)   # rel in {"calls", "imports"}
  symbols_fts: FTS5 index over (name, kind, file, signature), rowid = symbols.id
  events(type, timestamp, source, repo, commit?, file?, symbol?, payload JSON)
  events_fts: FTS5 index over (type, symbol, file, payload_text), rowid = events.id
  transcript_messages(id, session_id, role, content, timestamp, metadata JSON, source_id)
  transcript_messages_fts: FTS5 index over (session_id, role, content), rowid = transcript_messages.id
  tasks(id, title, subsystem?)
  event_evidence(event_id, transcript_id? or git_commit + git_path)

All queries are parameterized. All results are bounded (default 10, max 50).
FTS5 is an implementation detail of this backend; callers only see dicts.
"""

from __future__ import annotations

import json
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
CREATE TABLE IF NOT EXISTS indexed_code_files (
    file TEXT PRIMARY KEY NOT NULL
);
CREATE TABLE IF NOT EXISTS code_links (
    source_file TEXT NOT NULL REFERENCES indexed_code_files(file),
    target_file TEXT NOT NULL REFERENCES indexed_code_files(file),
    kind TEXT NOT NULL CHECK(kind IN ('imports', 're-exports', 'calls')),
    source_line INTEGER NOT NULL CHECK(source_line > 0),
    target_symbol TEXT NOT NULL,
    target_line INTEGER NOT NULL CHECK(target_line >= 0),
    PRIMARY KEY (source_file, target_file, kind, source_line, target_symbol, target_line)
);
CREATE INDEX IF NOT EXISTS idx_code_links_target ON code_links(target_file, source_file);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file, line);
CREATE INDEX IF NOT EXISTS idx_relationships_file ON relationships(file);
CREATE TABLE IF NOT EXISTS structural_fragments (
    file TEXT PRIMARY KEY NOT NULL,
    parser_version INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_nodes (
    id TEXT PRIMARY KEY NOT NULL,
    parent_id TEXT REFERENCES graph_nodes(id) DEFERRABLE INITIALLY DEFERRED,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    path TEXT NOT NULL,
    language TEXT,
    line INTEGER,
    byte_column INTEGER,
    end_line INTEGER,
    end_byte_column INTEGER
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_parent ON graph_nodes(parent_id, kind, name, id);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_name ON graph_nodes(name, path, line, id);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_qualified ON graph_nodes(qualified_name, path, line, id);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_path ON graph_nodes(path, kind, id);
CREATE TABLE IF NOT EXISTS graph_edges (
    id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL REFERENCES graph_nodes(id) DEFERRABLE INITIALLY DEFERRED,
    target_id TEXT NOT NULL REFERENCES graph_nodes(id) DEFERRABLE INITIALLY DEFERRED,
    kind TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK(confidence IN ('explicit', 'resolved')),
    path TEXT NOT NULL,
    line INTEGER,
    byte_column INTEGER,
    label TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_id, kind, line, byte_column, id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_id, kind, path, line, byte_column, id);
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
    payload TEXT NOT NULL DEFAULT '{}',
    source_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol);
CREATE INDEX IF NOT EXISTS idx_events_graph_file ON events(replace(file, char(92), '/'), id);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    type, symbol, file, payload_text
);
CREATE TABLE IF NOT EXISTS transcript_messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    source_id TEXT
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
CREATE TABLE IF NOT EXISTS monitor_cursors (
    project_name TEXT NOT NULL,
    source_table TEXT NOT NULL,
    last_rowid INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_name, source_table)
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY NOT NULL,
    title TEXT NOT NULL,
    subsystem TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_subsystem ON tasks(subsystem, title, id);
CREATE TABLE IF NOT EXISTS event_evidence (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    transcript_id INTEGER REFERENCES transcript_messages(id) ON DELETE CASCADE,
    git_commit TEXT,
    git_path TEXT,
    CHECK (
        (transcript_id IS NOT NULL AND git_commit IS NULL AND git_path IS NULL)
        OR (transcript_id IS NULL AND git_commit IS NOT NULL AND git_path IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_event_evidence_event ON event_evidence(event_id, id);
CREATE INDEX IF NOT EXISTS idx_event_evidence_transcript ON event_evidence(transcript_id);
CREATE INDEX IF NOT EXISTS idx_event_evidence_git_path ON event_evidence(git_path, event_id) WHERE git_path IS NOT NULL;
CREATE TABLE IF NOT EXISTS test_capture_policy (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled_since_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_sessions (
    transcript_id INTEGER PRIMARY KEY REFERENCES transcript_messages(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id)
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the workspace-local database (parents created on demand)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    # Serialize schema detection and DDL so two processes can safely upgrade the same database.
    with conn:
        conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
        _migrate_transcript_source_id(conn)
        _migrate_events_source_id(conn)
        # Track this additive migration by schema shape. Older TRACE stamps user_version=2.
        if not has_task_schema(conn):
            conn.execute("ALTER TABLE events ADD COLUMN task_id TEXT REFERENCES tasks(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(task_id, id) WHERE task_id IS NOT NULL")
        if not has_evidence_schema(conn):
            conn.execute("ALTER TABLE event_evidence ADD COLUMN reference TEXT")
            conn.execute("UPDATE event_evidence SET reference = 'ev_' || lower(hex(randomblob(16)))")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_reference ON event_evidence(reference)")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS assign_evidence_reference AFTER INSERT ON event_evidence
            WHEN NEW.reference IS NULL BEGIN
                UPDATE event_evidence SET reference = 'ev_' || lower(hex(randomblob(16))) WHERE id = NEW.id;
            END
        """)


def has_task_schema(conn: sqlite3.Connection) -> bool:
    return "task_id" in _table_columns(conn, "events")


def ensure_task_schema(conn: sqlite3.Connection) -> None:
    if not has_task_schema(conn):
        if conn.in_transaction:
            raise RuntimeError("initialize the task schema before starting a write transaction")
        init_schema(conn)


def ensure_capture_schema(conn: sqlite3.Connection) -> None:
    if not has_session_bindings(conn):
        init_schema(conn)


def has_session_bindings(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_sessions'").fetchone() is not None


def has_evidence_schema(conn: sqlite3.Connection) -> bool:
    return "reference" in _table_columns(conn, "event_evidence")


def ensure_evidence_schema(conn: sqlite3.Connection) -> None:
    if not has_evidence_schema(conn):
        if conn.in_transaction:
            raise RuntimeError("initialize evidence references before starting a write transaction")
        init_schema(conn)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


SCHEMA_VERSION = 2


def canonical_source_id(
    source_id: str | None,
    session_id: str,
    role: str,
    content: str,
    metadata: dict,
) -> str | None:
    """The one source-id format shared by migration and capture.

    ``part:<id>`` / ``message:<id>`` / ``session:<id>``. Older captured rows
    carry the upstream id only in metadata (or, from an earlier unreleased
    migration, as a bare id); both normalize to the same key, so a replay of
    an already-captured part can never insert a second row.
    """
    if source_id and ":" in source_id:
        return source_id
    meta = metadata if isinstance(metadata, dict) else {}
    # Only metadata shapes the capture path writes; a manual record that
    # happens to carry one of these keys is left unconstrained.
    if meta.get("part_id") and meta.get("part_type"):
        return f"part:{meta['part_id']}"
    if meta.get("message_id") and set(meta) == {"message_id"}:
        return f"message:{meta['message_id']}"
    if (
        role == "system"
        and meta.get("agent") == "opencode"
        and isinstance(content, str)
        and content.startswith("Session started")
    ):
        return f"session:{session_id}"
    return source_id or None


def _migrate_transcript_source_id(conn: sqlite3.Connection) -> None:
    """Add ``transcript_messages.source_id`` (canonical format) + uniqueness.

    Replaying a captured OpenCode row after an abrupt stop must be a no-op,
    so every captured row needs a stable upstream identity backed by a
    unique index. Data-preserving: rows without an identifiable source
    (e.g. ``trace transcript record``) are untouched; only rows that
    provably came from the same upstream record are collapsed, keeping the
    earliest, and their FTS rows go with them. Runs once per database
    (``PRAGMA user_version``).
    """
    if "source_id" not in _table_columns(conn, "transcript_messages"):
        conn.execute("ALTER TABLE transcript_messages ADD COLUMN source_id TEXT")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        keep: dict[str, int] = {}
        updates: list[tuple[str, int]] = []
        drop: list[int] = []
        rows = conn.execute(
            "SELECT id, session_id, role, content, metadata, source_id"
            " FROM transcript_messages ORDER BY id"
        ).fetchall()
        for row in rows:
            try:
                meta = json.loads(row[4]) if row[4] else {}
            except (TypeError, ValueError):
                meta = {}
            key = canonical_source_id(row[5], row[1], row[2], row[3], meta)
            if not key:
                continue
            if key in keep:
                drop.append(row[0])
                continue
            keep[key] = row[0]
            if key != row[5]:
                updates.append((key, row[0]))
        for row_id in drop:
            conn.execute("DELETE FROM transcript_messages WHERE id = ?", (row_id,))
            conn.execute("DELETE FROM transcript_messages_fts WHERE rowid = ?", (row_id,))
        for key, row_id in updates:
            conn.execute(
                "UPDATE transcript_messages SET source_id = ? WHERE id = ?", (key, row_id)
            )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_transcript_source_id "
        "ON transcript_messages(source_id) "
        "WHERE source_id IS NOT NULL AND source_id <> ''"
    )


def _migrate_events_source_id(conn: sqlite3.Connection) -> None:
    """Add ``events.source_id`` so captured events replay idempotently."""
    if "source_id" not in _table_columns(conn, "events"):
        conn.execute("ALTER TABLE events ADD COLUMN source_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_source_id "
        "ON events(source_id) WHERE source_id IS NOT NULL AND source_id <> ''"
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def clear(conn: sqlite3.Connection) -> None:
    """Wipe structural rows so re-indexing is deterministic (events kept)."""
    conn.execute("DELETE FROM symbols")
    conn.execute("DELETE FROM relationships")
    conn.execute("DELETE FROM symbols_fts")
    conn.execute("DELETE FROM code_links")
    conn.execute("DELETE FROM indexed_code_files")
    conn.execute("DELETE FROM graph_edges")
    conn.execute("DELETE FROM graph_nodes")
    conn.execute("DELETE FROM structural_fragments")
    conn.execute("DELETE FROM structural_fingerprint")


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


def clamp_limit(limit: object) -> int:
    if isinstance(limit, bool):
        return DEFAULT_LIMIT
    try:
        value = int(limit)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
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


def get_monitor_cursor(conn: sqlite3.Connection, project_name: str, source_table: str) -> int:
    """Get the last processed rowid for a monitor source table."""
    row = conn.execute(
        "SELECT last_rowid FROM monitor_cursors WHERE project_name = ? AND source_table = ?",
        (project_name, source_table),
    ).fetchone()
    return row[0] if row else 0


def set_monitor_cursor(conn: sqlite3.Connection, project_name: str, source_table: str, last_rowid: int) -> None:
    """Store the last processed rowid for a monitor source table."""
    import datetime
    updated_at = datetime.datetime.utcnow().isoformat() + "Z"
    conn.execute(
        "INSERT OR REPLACE INTO monitor_cursors (project_name, source_table, last_rowid, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (project_name, source_table, last_rowid, updated_at),
    )
    conn.commit()


def set_monitor_cursors(conn: sqlite3.Connection, project_name: str, cursors: dict) -> None:
    """Store all monitor cursors and commit (with any pending captured rows)."""
    import datetime
    updated_at = datetime.datetime.utcnow().isoformat() + "Z"
    for source_table, last_rowid in cursors.items():
        conn.execute(
            "INSERT OR REPLACE INTO monitor_cursors (project_name, source_table, last_rowid, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (project_name, source_table, int(last_rowid), updated_at),
        )
    conn.commit()


def get_all_monitor_cursors(conn: sqlite3.Connection, project_name: str) -> dict[str, int]:
    """Get all monitor cursors for a project."""
    rows = conn.execute(
        "SELECT source_table, last_rowid FROM monitor_cursors WHERE project_name = ?",
        (project_name,),
    ).fetchall()
    return {row[0]: row[1] for row in rows}


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


class SearchQueryError(ValueError):
    """A search query that cannot be run (as opposed to one with no hits)."""


def fts_query(query: str) -> str:
    """Turn user text into a literal FTS5 query: every term must match.

    Each whitespace-separated term is quoted, so identifiers such as
    ``refresh-token``, ``v1.2.3`` or ``foo.bar()`` match literally instead of
    being parsed as FTS5 syntax. Uppercase ``AND``/``OR``/``NOT`` between
    terms stay operators; a trailing ``*`` keeps prefix search. Raises
    SearchQueryError when nothing searchable remains (e.g. ``"()"``) or the
    operators are malformed, so callers can tell an unusable query from a
    query with no results.
    """
    terms = []
    for raw in (query or "").split():
        if raw in ("AND", "OR", "NOT"):
            terms.append(raw)
            continue
        prefix = raw.endswith("*") and len(raw) > 1
        term = raw[:-1] if prefix else raw
        if not any(ch.isalnum() for ch in term):
            continue
        quoted = '"' + term.replace('"', '""') + '"'
        if terms and terms[-1] not in ("AND", "OR", "NOT"):
            terms.append("AND")
        terms.append(quoted + ("*" if prefix else ""))
    # Dangling operators (leading, trailing, doubled) are noise, not syntax.
    ops = ("AND", "OR", "NOT")
    cleaned: list[str] = []
    for term in terms:
        if term in ops and (not cleaned or cleaned[-1] in ops):
            continue
        cleaned.append(term)
    while cleaned and cleaned[-1] in ops:
        cleaned.pop()
    if not cleaned:
        raise SearchQueryError(f"search query {query!r} has no searchable terms")
    return " ".join(cleaned)


def search_symbols(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    """Lexical search over name/kind/file/signature, best match first.

    Terms are matched literally (see fts_query). An unusable query raises
    SearchQueryError; a valid query with no hits returns [].
    """
    if not query or not query.strip():
        return []
    match = fts_query(query)
    try:
        rows = conn.execute(
            "SELECT s.name, s.kind, s.file, s.line, s.signature"
            " FROM symbols_fts JOIN symbols s ON s.id = symbols_fts.rowid"
            " WHERE symbols_fts MATCH ?"
            " ORDER BY bm25(symbols_fts), s.id LIMIT ?",
            (match, clamp_limit(limit)),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise SearchQueryError(f"search query {query!r} failed: {exc}") from exc
    return _rows_to_dicts(rows)
