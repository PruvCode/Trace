"""Episodic event model + record/search (Phase 3.1).

Sources, never confused:
- source="agent": an observation or claim from a coding session. Useful, but
  NOT ground truth.
- source="git": a deterministic derivation from repository history.
- source="capture": tool activity recorded automatically by the OpenCode
  capture service (what the agent looked up). Searchable, but never
  presented as a finding.

Timestamps are injectable so tests never depend on wall-clock time.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from memory import redact as redact_mod
from memory import store as store_mod


EVENT_TYPES = frozenset(
    {"investigation", "attempt", "decision", "observation", "git_change"}
)
SOURCES = frozenset({"agent", "git", "capture"})

# Optional lifecycle, kept in the payload so no schema change is needed and
# historical events (no status) stay valid:
#   payload["status"]: how much the recorder stands behind the claim.
#   payload["supersedes"]: ids of earlier events this one replaces.
# "superseded" is derived (a later event names it); never stored.
STATUSES = frozenset({"hypothesis", "observed", "verified"})


def _supersedes_ids(payload: dict) -> list[int]:
    value = payload.get("supersedes")
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    if not values or not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in values):
        raise ValueError("supersedes must be a positive event id or a list of them")
    return values


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
    superseded_by: int | None = None


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
    source_id: str | None = None,
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
    status = payload.get("status")
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {sorted(STATUSES)}")
    _supersedes_ids(payload)
    payload = redact_mod.redact_value(payload)
    try:
        payload_text = json.dumps(payload, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload is not JSON-serializable: {exc}") from exc
    return {
        "type": type,
        "timestamp": timestamp or utcnow_iso(),
        "source": source,
        "repo": redact_mod.redact_text(repo.strip()),
        "commit": redact_mod.redact_text(commit.strip()) if commit else None,
        "file": redact_mod.redact_text(file.strip()) if file else None,
        "symbol": redact_mod.redact_text(symbol.strip()) if symbol else None,
        "payload": payload,
        "payload_text": payload_text,
        "source_id": source_id.strip() if isinstance(source_id, str) and source_id.strip() else None,
    }


def record_event(conn: sqlite3.Connection, autocommit: bool = True, **fields) -> int:
    """Validate, persist, and index one event; return its id.

    With a ``source_id`` (the upstream record it was captured from),
    recording the same source again is a no-op returning the existing id.
    """
    event = validate_event(**fields)
    for old_id in _supersedes_ids(event["payload"]):
        if conn.execute("SELECT 1 FROM events WHERE id = ?", (old_id,)).fetchone() is None:
            raise ValueError(f"supersedes refers to unknown event id {old_id} in this project")
    values = (
        event["type"],
        event["timestamp"],
        event["source"],
        event["repo"],
        event["commit"],
        event["file"],
        event["symbol"],
        event["payload_text"],
    )
    columns = "type, timestamp, source, repo, commit_sha, file, symbol, payload"
    if event["source_id"] is None:
        # Plain insert: also works on databases not yet migrated.
        cursor = conn.execute(f"INSERT INTO events ({columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values)
    else:
        cursor = conn.execute(
            f"INSERT INTO events ({columns}, source_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source_id) WHERE source_id IS NOT NULL AND source_id <> '' DO NOTHING",
            (*values, event["source_id"]),
        )
    if cursor.rowcount == 0:
        row = conn.execute(
            "SELECT id FROM events WHERE source_id = ?", (event["source_id"],)
        ).fetchone()
        return row[0]
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
    if autocommit:
        conn.commit()
    return event_id


def superseded_map(conn: sqlite3.Connection) -> dict[int, int]:
    """Map each superseded event id to the newest event that supersedes it."""
    result: dict[int, int] = {}
    rows = conn.execute(
        "SELECT id, payload FROM events WHERE payload LIKE '%\"supersedes\"%' ORDER BY id"
    ).fetchall()
    for event_id, payload in rows:
        try:
            ids = _supersedes_ids(json.loads(payload))
        except (ValueError, TypeError):
            continue
        for old_id in ids:
            if old_id != event_id:
                result[old_id] = event_id
    return result


def _row_to_event(row: sqlite3.Row, superseded: dict[int, int] | None = None) -> Event:
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
        superseded_by=(superseded or {}).get(row["id"]),
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
    """Narrow bounded retrieval.

    Without a text query results are insertion-ordered. With one, the best
    match comes first and newer events break ties. Query terms match
    literally; an unusable query raises store.SearchQueryError.
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
    join = ""
    order = "e.id"
    if query is not None and query.strip():
        join = " JOIN events_fts ON events_fts.rowid = e.id"
        clauses.append("events_fts MATCH ?")
        params.append(store_mod.fts_query(query))
        order = "bm25(events_fts), e.id DESC"
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        rows = conn.execute(
            "SELECT e.id, e.type, e.timestamp, e.source, e.repo,"
            " e.commit_sha, e.file, e.symbol, e.payload"
            f" FROM events e{join} {where} ORDER BY {order} LIMIT ?",
            (*params, store_mod.clamp_limit(limit)),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise store_mod.SearchQueryError(f"event search failed: {exc}") from exc
    superseded = superseded_map(conn)
    return [_row_to_event(row, superseded) for row in rows]
