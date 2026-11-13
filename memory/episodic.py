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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from memory import redact as redact_mod
from memory import store as store_mod
from memory import task_library


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
MAX_ACCEPTANCE_CHECKS = 6
MAX_ACCEPTANCE_CHARS = 180


def acceptance_checks(payload: dict) -> list[str]:
    """Return validated, bounded descriptions of expected behavior."""
    if "acceptance" not in payload:
        return []
    checks = payload["acceptance"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= MAX_ACCEPTANCE_CHECKS:
        raise ValueError(f"acceptance must contain 1 to {MAX_ACCEPTANCE_CHECKS} checks")
    normalized = []
    for check in checks:
        if (not isinstance(check, str) or not 1 <= len(check.strip()) <= MAX_ACCEPTANCE_CHARS
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in check)):
            raise ValueError(f"each acceptance check must be one line of up to {MAX_ACCEPTANCE_CHARS} characters")
        normalized.append(check.strip())
    return list(dict.fromkeys(normalized))


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
    task_id: str | None = None
    evidence: list[dict] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)

    def as_record(self) -> dict:
        record = asdict(self)
        if self.task_id is None:
            del record["task_id"]
        if not self.evidence:
            del record["evidence"]
        if not self.evidence_refs:
            del record["evidence_refs"]
        return record


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
    if "acceptance" in payload:
        if type != "decision":
            raise ValueError("acceptance checks belong on decisions")
        payload = {**payload, "acceptance": acceptance_checks(payload)}
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


def record_event(
    conn: sqlite3.Connection,
    autocommit: bool = True,
    *,
    task_id: str | None = None,
    evidence: list[dict] | None = None,
    project_root: Path | None = None,
    **fields,
) -> int:
    """Validate, persist, and index one event; return its id.

    With a ``source_id`` (the upstream record it was captured from),
    recording the same source again is a no-op returning the existing id.
    """
    event = validate_event(**fields)
    if task_id is not None:
        task_library.get_task(conn, task_id)
    if evidence:
        store_mod.ensure_evidence_schema(conn)
    links = task_library.evidence_links(conn, evidence, project_root)
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT record_event")
    try:
        event_id, inserted = _insert_event(conn, event, task_id, links)
    except BaseException:
        conn.execute("ROLLBACK TO record_event")
        conn.execute("RELEASE record_event")
        if started:
            conn.rollback()
        raise
    conn.execute("RELEASE record_event")
    if autocommit and inserted:
        conn.commit()
    elif started and not inserted:
        conn.rollback()
    return event_id


def _insert_event(
    conn: sqlite3.Connection,
    event: dict,
    task_id: str | None,
    links: list[tuple[int | None, str | None, str | None]],
) -> tuple[int, bool]:
    for old_id in _supersedes_ids(event["payload"]):
        row = conn.execute("SELECT * FROM events WHERE id = ?", (old_id,)).fetchone()
        if row is None:
            raise ValueError(f"supersedes refers to unknown event id {old_id} in this project")
        if (row["task_id"] if "task_id" in row.keys() else None) != task_id:
            raise ValueError("a replacement and the event it supersedes must have the same task scope")
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
    if task_id is not None:
        columns += ", task_id"
        values += (task_id,)
    placeholders = ", ".join("?" for _ in values)
    if event["source_id"] is None:
        # Plain insert: also works on databases not yet migrated.
        cursor = conn.execute(f"INSERT INTO events ({columns}) VALUES ({placeholders})", values)
    else:
        cursor = conn.execute(
            f"INSERT INTO events ({columns}, source_id) VALUES ({placeholders}, ?)"
            " ON CONFLICT(source_id) WHERE source_id IS NOT NULL AND source_id <> '' DO NOTHING",
            (*values, event["source_id"]),
        )
    if cursor.rowcount == 0:
        row = conn.execute(
            "SELECT * FROM events WHERE source_id = ?", (event["source_id"],)
        ).fetchone()
        if task_id is not None and row["task_id"] != task_id:
            raise ValueError("source ID already belongs to an event with a different task scope")
        return row["id"], False
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
    if links:
        conn.executemany(
            "INSERT INTO event_evidence (event_id, transcript_id, git_commit, git_path) VALUES (?, ?, ?, ?)",
            [(event_id, *link) for link in links],
        )
    return event_id, True


def superseded_map(conn: sqlite3.Connection, task_id: str | None = None) -> dict[int, int]:
    """Map each superseded event id to the newest event that supersedes it."""
    result: dict[int, int] = {}
    scope = " AND task_id = ?" if task_id is not None else ""
    rows = conn.execute(
        f"SELECT id, payload FROM events WHERE payload LIKE '%\"supersedes\"%'{scope} ORDER BY id",
        (task_id,) if task_id is not None else (),
    )
    for event_id, payload in rows:
        try:
            ids = _supersedes_ids(json.loads(payload))
        except (ValueError, TypeError):
            continue
        for old_id in ids:
            if old_id != event_id:
                result[old_id] = event_id
    return result


def _evidence_by_event(conn: sqlite3.Connection, rows: list[sqlite3.Row], limit_per_event: int | None = None) -> dict[int, list[dict]]:
    if not rows or "task_id" not in rows[0].keys():
        return {}
    placeholders = ", ".join("?" for _ in rows)
    reference = "reference" if store_mod.has_evidence_schema(conn) else "NULL"
    query = (f"SELECT event_id, transcript_id, git_commit, git_path, {reference} AS ref, id FROM event_evidence"
             f" WHERE event_id IN ({placeholders})")
    params = tuple(row["id"] for row in rows)
    if limit_per_event is None:
        links = conn.execute(f"SELECT event_id, transcript_id, git_commit, git_path, ref FROM ({query}) ORDER BY id", params)
    else:
        links = conn.execute(f"""
            SELECT event_id, transcript_id, git_commit, git_path, ref FROM (
                SELECT *, row_number() OVER (PARTITION BY event_id ORDER BY id) AS rank FROM ({query})
            ) WHERE rank <= ? ORDER BY id
        """, (*params, limit_per_event))
    result: dict[int, list[dict]] = {}
    for event_id, transcript_id, git_commit, git_path, ref in links:
        link = (
            {"type": "transcript", "message_id": transcript_id} if transcript_id is not None else
            {"type": "git", "commit": git_commit, "path": git_path}
        )
        link["reference"] = ref
        result.setdefault(event_id, []).append(link)
    return result


def _row_to_event(row: sqlite3.Row, superseded: dict[int, int], evidence: dict[int, list[dict]]) -> Event:
    links = evidence.get(row["id"], [])
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
        superseded_by=superseded.get(row["id"]),
        task_id=row["task_id"] if "task_id" in row.keys() else None,
        evidence=[{k: v for k, v in link.items() if k != "reference"} for link in links],
        evidence_refs=[link["reference"] for link in links if link["reference"]],
    )


def code_file_history(conn: sqlite3.Connection) -> dict[str, list[Event]]:
    """Bounded export of records explicitly attached to indexed paths."""
    rows = conn.execute("""
        WITH attached AS (
            SELECT f.file AS path, e.id FROM events e
            JOIN indexed_code_files f ON f.file = replace(e.file, char(92), '/')
            UNION
            SELECT f.file, g.event_id FROM event_evidence g
            JOIN indexed_code_files f ON f.file = g.git_path
        ), ranked AS (
            SELECT path, id, row_number() OVER (PARTITION BY path ORDER BY id DESC) AS rank
            FROM attached
        )
        SELECT e.*, r.path AS attached_path FROM ranked r JOIN events e ON e.id = r.id
        WHERE r.rank <= 6 ORDER BY r.rank, r.path, e.id DESC LIMIT 800
    """).fetchall()
    superseded = superseded_map(conn, None) if rows else {}
    links = _evidence_by_event(conn, rows)
    result: dict[str, list[Event]] = {}
    for row in rows:
        result.setdefault(row["attached_path"], []).append(_row_to_event(row, superseded, links))
    return result


def search_events(
    conn: sqlite3.Connection,
    *,
    type: str | None = None,
    query: str | None = None,
    symbol: str | None = None,
    file: str | None = None,
    commit: str | None = None,
    limit: int = 10,
    task_id: str | None = None,
    unassigned_only: bool = False,
    newest_first: bool = False,
    ids: list[int] | None = None,
    decision_only: bool = False,
    captured_failure_only: bool = False,
    normalize_file: bool = False,
) -> list[Event]:
    """Narrow bounded retrieval.

    Without a text query results are insertion-ordered. With one, the best
    match comes first and newer events break ties. Query terms match
    literally; an unusable query raises store.SearchQueryError.
    """
    clauses: list[str] = []
    params: list = []
    if ids is not None:
        if not ids:
            return []
        clauses.append("e.id IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(ids[:store_mod.MAX_LIMIT]))
    if decision_only:
        clauses.append("(e.type='decision' OR json_extract(e.payload, '$.status')='verified')")
    if captured_failure_only:
        clauses.append("(e.source='capture' AND CASE WHEN json_valid(e.payload) THEN "
                       "json_extract(e.payload, '$.kind')='test_outcome' AND "
                       "json_array_length(e.payload, '$.failed_checks') > 0 ELSE 0 END)")
    if task_id is not None:
        task_library.get_task(conn, task_id)
        clauses.append("e.task_id = ?")
        params.append(task_id)
    elif unassigned_only and store_mod.has_task_schema(conn):
        clauses.append("e.task_id IS NULL")
    if type is not None:
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type filter {type!r}")
        clauses.append("e.type = ?")
        params.append(type)
    for column, value in (
        ("symbol", symbol),
        ("commit_sha", commit),
    ):
        if value is not None:
            clauses.append(f"e.{column} = ?")
            params.append(value)
    if file is not None:
        clauses.append("replace(e.file, char(92), '/') = ?" if normalize_file else "e.file = ?")
        params.append(file.replace("\\", "/") if normalize_file else file)
    join = ""
    order = "e.id DESC" if newest_first else "e.id"
    if query is not None and query.strip():
        join = " JOIN events_fts ON events_fts.rowid = e.id"
        clauses.append("events_fts MATCH ?")
        params.append(store_mod.fts_query(query))
        order = "bm25(events_fts), e.id DESC"
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        rows = conn.execute(
            "SELECT e.*"
            f" FROM events e{join} {where} ORDER BY {order} LIMIT ?",
            (*params, store_mod.clamp_limit(limit)),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise store_mod.SearchQueryError(f"event search failed: {exc}") from exc
    superseded = superseded_map(conn, task_id)
    evidence = _evidence_by_event(conn, rows)
    return [_row_to_event(row, superseded, evidence) for row in rows]
