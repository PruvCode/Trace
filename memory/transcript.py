"""Transcript message model + record/search (Phase 4: transcript memory).

Minimal model: session_id, role, content, timestamp, metadata.
FTS5 for search, bounded results, deterministic ordering.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from memory import redact as redact_mod
from memory import store as store_mod

ROLES = frozenset({"user", "assistant", "system", "tool"})


@dataclass(frozen=True)
class TranscriptMessage:
    id: int
    session_id: str
    role: str
    content: str
    timestamp: str
    metadata: dict = field(default_factory=dict)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_message(
    *,
    session_id: str,
    role: str,
    content: str,
    timestamp: str | None = None,
    metadata: dict | None = None,
    source_id: str | None = None,
) -> dict:
    """Normalize one transcript message or raise ValueError describing the problem."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {sorted(ROLES)}")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a dict or None")
    if source_id is not None and (not isinstance(source_id, str) or not source_id.strip()):
        raise ValueError("source_id must be a non-empty string or None")
    try:
        metadata_text = json.dumps(redact_mod.redact_value(metadata), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"metadata is not JSON-serializable: {exc}") from exc
    return {
        "session_id": session_id.strip(),
        "role": role,
        "content": redact_mod.redact_text(content),
        "timestamp": timestamp or utcnow_iso(),
        "metadata": metadata,
        "metadata_text": metadata_text,
        "source_id": source_id.strip() if source_id else None,
    }


def record_message(conn: sqlite3.Connection, autocommit: bool = True, **fields) -> int:
    """Validate, persist, and index one transcript message; return its id.

    ``source_id`` is the stable identity of the upstream record (an OpenCode
    part/message/session id). When present, recording the same source twice is
    a no-op: the second call returns the existing row's id instead of
    inserting a duplicate. This makes replay after an interrupted capture
    safe even when the monitor's ROWID cursor has rolled back.

    Without a ``source_id`` the call behaves as before and always inserts,
    because nothing can distinguish a genuine repeat from a replay.
    ``autocommit=False`` leaves the transaction open for a caller that batches.
    """
    msg = validate_message(**fields)
    values = (msg["session_id"], msg["role"], msg["content"], msg["timestamp"], msg["metadata_text"])
    columns = "session_id, role, content, timestamp, metadata"
    if msg["source_id"] is None:
        # Plain insert: also works on databases not yet migrated.
        cursor = conn.execute(
            f"INSERT INTO transcript_messages ({columns}) VALUES (?, ?, ?, ?, ?)", values
        )
    else:
        cursor = conn.execute(
            f"INSERT INTO transcript_messages ({columns}, source_id) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source_id) WHERE source_id IS NOT NULL AND source_id <> '' DO NOTHING",
            (*values, msg["source_id"]),
        )
    if cursor.rowcount == 0:
        # Already recorded from this source. Return the existing id.
        row = conn.execute(
            "SELECT id FROM transcript_messages WHERE source_id = ?", (msg["source_id"],)
        ).fetchone()
        return row[0]
    msg_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO transcript_messages_fts (rowid, session_id, role, content)"
        " VALUES (?, ?, ?, ?)",
        (
            msg_id,
            msg["session_id"],
            msg["role"],
            msg["content"],
        ),
    )
    if autocommit:
        conn.commit()
    return msg_id


def _row_to_message(row: sqlite3.Row) -> TranscriptMessage:
    return TranscriptMessage(
        id=row["id"],
        session_id=row["session_id"],
        role=row["role"],
        content=row["content"],
        timestamp=row["timestamp"],
        metadata=json.loads(row["metadata"]),
    )


def search_messages(
    conn: sqlite3.Connection,
    *,
    session_id: str | None = None,
    role: str | None = None,
    query: str | None = None,
    limit: int = 10,
) -> list[TranscriptMessage]:
    """Narrow bounded retrieval.

    Without a text query results are insertion-ordered. With one, the best
    match comes first and newer messages break ties. Query terms match
    literally; an unusable query raises store.SearchQueryError.
    """
    if role is not None and role not in ROLES:
        raise ValueError(f"unknown role filter {role!r}; expected one of {sorted(ROLES)}")

    clauses: list[str] = []
    params: list = []
    if session_id is not None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string or None")
        clauses.append("tm.session_id = ?")
        params.append(session_id.strip())
    if role is not None:
        clauses.append("tm.role = ?")
        params.append(role)
    join = ""
    order = "tm.id"
    if query is not None and query.strip():
        join = " JOIN transcript_messages_fts ON transcript_messages_fts.rowid = tm.id"
        clauses.append("transcript_messages_fts MATCH ?")
        params.append(store_mod.fts_query(query))
        order = "bm25(transcript_messages_fts), tm.id DESC"
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        rows = conn.execute(
            "SELECT tm.id, tm.session_id, tm.role, tm.content, tm.timestamp, tm.metadata"
            f" FROM transcript_messages tm{join} {where} ORDER BY {order} LIMIT ?",
            (*params, store_mod.clamp_limit(limit)),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise store_mod.SearchQueryError(f"transcript search failed: {exc}") from exc
    return [_row_to_message(row) for row in rows]


def get_session_messages(
    conn: sqlite3.Connection,
    session_id: str,
    limit: int = 50,
) -> list[TranscriptMessage]:
    """Retrieve all messages for a session, ordered by insertion (deterministic)."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
    rows = conn.execute(
        "SELECT id, session_id, role, content, timestamp, metadata"
        " FROM transcript_messages"
        " WHERE session_id = ? ORDER BY id LIMIT ?",
        (session_id.strip(), store_mod.clamp_limit(limit)),
    ).fetchall()
    return [_row_to_message(row) for row in rows]


# Captured OpenCode parts that are status markers, not conversation.
NON_CONVERSATIONAL_PART_TYPES = ("step-finish", "step-start", "tool", "reasoning")


def get_recent_conversation(
    conn: sqlite3.Connection,
    session_id: str,
    limit: int = 10,
) -> list[TranscriptMessage]:
    """The newest ``limit`` user/assistant messages of a session, oldest first.

    Status rows captured from OpenCode (``step-finish`` reasons such as
    "tool-calls"/"stop", step markers) are excluded in SQL so they can never
    use up the budget, including rows stored by older capture versions.
    Text from an abandoned turn (``metadata.incomplete``) stays searchable
    but is never offered as conversation: it can stop mid-sentence.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
    placeholders = ", ".join("?" for _ in NON_CONVERSATIONAL_PART_TYPES)
    rows = conn.execute(
        "SELECT id, session_id, role, content, timestamp, metadata"
        " FROM transcript_messages"
        " WHERE session_id = ? AND role IN ('user', 'assistant')"
        " AND trim(content) <> ''"
        " AND COALESCE(json_extract(CASE WHEN json_valid(metadata) THEN metadata END,"
        f" '$.part_type'), '') NOT IN ({placeholders})"
        " AND NOT COALESCE(json_extract(CASE WHEN json_valid(metadata) THEN metadata END,"
        " '$.incomplete'), 0)"
        " ORDER BY id DESC LIMIT ?",
        (session_id.strip(), *NON_CONVERSATIONAL_PART_TYPES, store_mod.clamp_limit(limit)),
    ).fetchall()
    return [_row_to_message(row) for row in reversed(rows)]


def session_parent(conn: sqlite3.Connection, session_id: str) -> str | None:
    """Parent session id recorded at capture time (subagent sessions), if any."""
    row = conn.execute(
        "SELECT metadata FROM transcript_messages WHERE session_id = ? AND role = 'system'"
        " AND source_id = ? LIMIT 1",
        (session_id, f"session:{session_id}"),
    ).fetchone()
    if not row:
        return None
    try:
        meta = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    parent = meta.get("parent_id") if isinstance(meta, dict) else None
    return parent or None


def list_sessions(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """List distinct session_ids with message counts, newest first."""
    rows = conn.execute(
        "SELECT session_id, COUNT(*) as count, MAX(timestamp) as last_activity"
        " FROM transcript_messages"
        " GROUP BY session_id"
        # Timestamps have one-second precision; ties go to the latest insert.
        " ORDER BY last_activity DESC, MAX(id) DESC"
        " LIMIT ?",
        (store_mod.clamp_limit(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def count_messages(conn: sqlite3.Connection) -> int:
    """Total transcript message count."""
    return conn.execute("SELECT COUNT(*) FROM transcript_messages").fetchone()[0]