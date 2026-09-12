"""Episodic event model + record/search (Phase 3.1).

Two sources, never confused:
- source="agent": an observation or claim from a coding session. Useful, but
  NOT ground truth.
- source="git": a deterministic derivation from repository history.

Timestamps are injectable so tests never depend on wall-clock time.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from memory import store as store_mod

EVENT_TYPES = frozenset(
    {"investigation", "attempt", "decision", "observation", "git_change"}
)
SOURCES = frozenset({"agent", "git"})


@dataclass(frozen=True)
class Event:
    id: int
    type: str
    timestamp: str
    source: str
    repo: str
    commit: str | None = None
    file: str | None = None
    symbol: str | None = None
    payload: dict = field(default_factory=dict)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_event(
    *,
    type: str,
    source: str = "agent",
    timestamp: str | None = None,
    repo: str,
    commit: str | None = None,
    file: str | None = None,
    symbol: str | None = None,
    payload: dict | None = None,
) -> dict:
    """Normalize one event or raise ValueError describing the problem."""
    if type not in EVENT_TYPES:
        raise ValueError(
            f"unknown event type {type!r}; expected one of {sorted(EVENT_TYPES)}"
        )
    if source not in SOURCES:
        raise ValueError(
            f"unknown event source {source!r}; expected one of {sorted(SOURCES)}"
        )
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("repo must be a non-empty repository identity string")
    for label, value in (("commit", commit), ("file", file), ("symbol", symbol)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{label} must be a non-empty string or None")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict or None")
    try:
        payload_text = json.dumps(payload, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload is not JSON-serializable: {exc}") from exc
    return {
        "type": type,
        "timestamp": timestamp or utcnow_iso(),
        "source": source,
        "repo": repo.strip(),
        "commit": commit.strip() if commit else None,
        "file": file.strip() if file else None,
        "symbol": symbol.strip() if symbol else None,
        "payload": payload,
        "payload_text": payload_text,
    }


def record_event(conn: sqlite3.Connection, **fields) -> int:
    """Validate, persist, and index one event; return its id."""
    event = validate_event(**fields)
    cursor = conn.execute(
        "INSERT INTO events (type, timestamp, source, repo, commit_sha, file,"
        " symbol, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event["type"],
            event["timestamp"],
            event["source"],
            event["repo"],
            event["commit"],
            event["file"],
            event["symbol"],
            event["payload_text"],
        ),
    )
    event_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO events_fts (rowid, type, symbol, file, payload_text)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            event_id,
            event["type"],
            event["symbol"] or "",
            event["file"] or "",
            event["payload_text"],
        ),
    )
    conn.commit()
    return event_id


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        type=row["type"],
        timestamp=row["timestamp"],
        source=row["source"],
        repo=row["repo"],
        commit=row["commit_sha"],
        file=row["file"],
        symbol=row["symbol"],
        payload=json.loads(row["payload"]),
    )


def search_events(
    conn: sqlite3.Connection,
    *,
    type: str | None = None,
    query: str | None = None,
    symbol: str | None = None,
    file: str | None = None,
    commit: str | None = None,
    limit: int = 10,
) -> list[Event]:
    """Narrow bounded retrieval. Insertion-ordered (deterministic).

    Malformed FTS queries return [] by design, mirroring search_symbols.
    """
    clauses: list[str] = []
    params: list = []
    if type is not None:
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type filter {type!r}")
        clauses.append("e.type = ?")
        params.append(type)
    for column, value in (
        ("symbol", symbol),
        ("file", file),
        ("commit_sha", commit),
    ):
        if value is not None:
            clauses.append(f"e.{column} = ?")
            params.append(value)
    if query is not None and query.strip():
        clauses.append(
            "e.id IN (SELECT rowid FROM events_fts WHERE events_fts MATCH ?)"
        )
        params.append(query.strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        rows = conn.execute(
            "SELECT e.id, e.type, e.timestamp, e.source, e.repo,"
            " e.commit_sha, e.file, e.symbol, e.payload"
            f" FROM events e {where} ORDER BY e.id LIMIT ?",
            (*params, store_mod.clamp_limit(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_row_to_event(row) for row in rows]
