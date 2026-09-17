"""Transcript message model + record/search (Phase 4: transcript memory).

Minimal model: session_id, role, content, timestamp, metadata.
FTS5 for search, bounded results, deterministic ordering.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

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
    try:
        metadata_text = json.dumps(metadata, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"metadata is not JSON-serializable: {exc}") from exc
    return {
        "session_id": session_id.strip(),
        "role": role,
        "content": content,
        "timestamp": timestamp or utcnow_iso(),
        "metadata": metadata,
        "metadata_text": metadata_text,
    }


def record_message(conn: sqlite3.Connection, **fields) -> int:
    """Validate, persist, and index one transcript message; return its id."""
    msg = validate_message(**fields)
    cursor = conn.execute(
        "INSERT INTO transcript_messages (session_id, role, content, timestamp, metadata)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            msg["session_id"],
            msg["role"],
            msg["content"],
            msg["timestamp"],
            msg["metadata_text"],
        ),
    )
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
    """Narrow bounded retrieval. Insertion-ordered (deterministic).

    Malformed FTS queries return [] by design, mirroring search_symbols.
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
    if query is not None and query.strip():
        clauses.append(
            "tm.id IN (SELECT rowid FROM transcript_messages_fts WHERE transcript_messages_fts MATCH ?)"
        )
        params.append(query.strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        rows = conn.execute(
            "SELECT tm.id, tm.session_id, tm.role, tm.content, tm.timestamp, tm.metadata"
            f" FROM transcript_messages tm {where} ORDER BY tm.id LIMIT ?",
            (*params, store_mod.clamp_limit(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
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


def list_sessions(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """List distinct session_ids with message counts, newest first."""
    rows = conn.execute(
        "SELECT session_id, COUNT(*) as count, MAX(timestamp) as last_activity"
        " FROM transcript_messages"
        " GROUP BY session_id"
        " ORDER BY last_activity DESC"
        " LIMIT ?",
        (store_mod.clamp_limit(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def count_messages(conn: sqlite3.Connection) -> int:
    """Total transcript message count."""
    return conn.execute("SELECT COUNT(*) FROM transcript_messages").fetchone()[0]