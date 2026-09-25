"""Bounded session-start context assembly for automatic retrieval.

At session start there is no user query yet, so retrieval is recency-based:
the tail of the most recent prior session plus recent episodic findings,
read from the current project's TRACE database only (isolation by
construction — no other project's database is ever opened).

Policy (Step 5.2):
- at most 10 user/assistant tail messages from the most recent prior session
- at most 5 recent episodic findings
- at most 20 items and 4000 characters total (deterministic truncation)
- current session excluded; empty memory injects nothing
- malformed rows skipped; any retrieval error fails open (empty result)
- secret-like values redacted with the shared capture-side rules
- no embeddings, no network, local SQLite only
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from memory import redact as redact_mod
from memory import store as store_mod
from memory import synthetic as synthetic_mod
from memory import transcript as transcript_mod

MAX_TAIL_MESSAGES = 10
MAX_FINDINGS = 5
MAX_ITEMS = 20
MAX_CHARS = 4000
MAX_ITEM_CHARS = 500


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _tail_messages(conn: sqlite3.Connection, exclude_session_id: str | None) -> tuple[list[dict], str | None]:
    """Collect the tail of the most recent prior session.

    Returns (items, session_id). Sessions are newest-first; the first
    session that is not the excluded current session and that has any
    user/assistant content wins. Malformed rows are skipped.
    """
    try:
        sessions = transcript_mod.list_sessions(conn, limit=50)
    except (sqlite3.Error, ValueError):
        return [], None
    for entry in sessions:
        try:
            session_id = entry.get("session_id")
        except AttributeError:
            continue
        if not session_id or session_id == exclude_session_id:
            continue
        try:
            messages = transcript_mod.get_session_messages(
                conn, session_id, limit=MAX_TAIL_MESSAGES * 2
            )
        except (sqlite3.Error, ValueError):
            continue
        tail = messages[-MAX_TAIL_MESSAGES:]
        items: list[dict] = []
        for msg in tail:
            try:
                role = msg.role
                content = (msg.content or "").strip()
            except AttributeError:
                continue
            if role not in ("user", "assistant") or not content:
                continue
            if synthetic_mod.is_synthetic_memory_text(content):
                # Injected memory must never be re-surfaced as prior content.
                continue
            text, _ = _truncate(content, MAX_ITEM_CHARS)
            items.append({"kind": "message", "role": role, "text": redact_mod.redact_text(text)})
        if items:
            return items, session_id
    return [], None


def _recent_findings(conn: sqlite3.Connection) -> list[dict]:
    """Collect the most recent episodic findings, newest first, bounded."""
    try:
        rows = conn.execute(
            "SELECT type, payload FROM events ORDER BY id DESC LIMIT ?",
            (MAX_FINDINGS,),
        ).fetchall()
    except sqlite3.Error:
        return []
    items: list[dict] = []
    for row in rows:
        try:
            event_type = row[0] or "event"
            payload = row[1] or ""
            try:
                data = json.loads(payload) if isinstance(payload, str) else payload
                if isinstance(data, dict):
                    payload_text = data.get("finding") or data.get("decision") or data.get("content") or payload
                else:
                    payload_text = payload
            except (ValueError, TypeError):
                payload_text = payload
            text = str(payload_text).strip()
            if not text:
                continue
            text, _ = _truncate(text, MAX_ITEM_CHARS)
            items.append({
                "kind": "finding",
                "role": str(event_type),
                "text": redact_mod.redact_text(text),
            })
        except (IndexError, TypeError, AttributeError):
            continue
    items.reverse()  # chronological within the block; overall order stays deterministic
    return items


def _apply_budgets(items: list[dict], max_items: int = MAX_ITEMS, max_chars: int = MAX_CHARS) -> tuple[list[dict], bool]:
    """Enforce item/character budgets deterministically.

    Items past the item budget are dropped from the end (findings are
    appended last, so they yield first). Character budget is enforced by
    dropping trailing items whole — never by re-slicing mid-item.
    """
    truncated = False
    if len(items) > max(0, max_items):
        items = items[:max(0, max_items)]
        truncated = True
    total = sum(len(item["text"]) for item in items)
    while items and total > max(0, max_chars):
        dropped = items.pop()
        total -= len(dropped["text"])
        truncated = True
    return items, truncated


def format_block(items: list[dict]) -> str:
    """Render items as a compact human-readable context block."""
    if not items:
        return ""
    lines = ["<TRACE MEMORY>", "Previous session:"]
    messages = [i for i in items if i["kind"] == "message"]
    findings = [i for i in items if i["kind"] == "finding"]
    for item in messages:
        lines.append(f"  - {item['role']}: {item['text']}")
    if findings:
        lines.append("")
        lines.append("Recent findings:")
        for item in findings:
            lines.append(f"  - {item['role']}: {item['text']}")
    lines.append("")
    lines.append("</TRACE MEMORY>")
    return "\n".join(lines)


def get_session_start_context(
    trace_db_path: str | Path,
    *,
    exclude_session_id: str | None = None,
    max_items: int = MAX_ITEMS,
    max_chars: int = MAX_CHARS,
) -> dict[str, Any]:
    """Assemble bounded prior-session context. Never raises for DB problems.

    Returns {"items", "text", "truncated", "stats"}. Empty memory, missing
    DB, or any retrieval error yields items=[] and text="".
    """
    started = time.perf_counter()
    stats: dict[str, Any] = {
        "prior_session": None,
        "message_items": 0,
        "finding_items": 0,
        "latency_ms": 0,
    }
    try:
        db_path = Path(trace_db_path)
        if not db_path.exists():
            return {"items": [], "text": "", "truncated": False, "stats": stats}
        conn = store_mod.connect(db_path)
        try:
            tail, prior_session = _tail_messages(conn, exclude_session_id)
            findings = _recent_findings(conn)
        finally:
            conn.close()
    except Exception:
        return {"items": [], "text": "", "truncated": False, "stats": stats}

    items = tail + findings
    items, truncated = _apply_budgets(items, max_items, max_chars)
    stats["prior_session"] = bool(prior_session)
    stats["message_items"] = sum(1 for i in items if i["kind"] == "message")
    stats["finding_items"] = sum(1 for i in items if i["kind"] == "finding")
    stats["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return {
        "items": items,
        "text": format_block(items),
        "truncated": truncated,
        "stats": stats,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI: print the session-start block (or nothing). Always fail-open."""
    parser = argparse.ArgumentParser(description="Assemble TRACE session-start context")
    parser.add_argument("--db", required=True, help="TRACE memory.db path")
    parser.add_argument("--exclude-session", default=None, help="Current session id to exclude")
    args = parser.parse_args(argv)
    try:
        result = get_session_start_context(args.db, exclude_session_id=args.exclude_session)
    except Exception:
        return 0
    stats = dict(result.get("stats", {}))
    stats["items"] = len(result.get("items", []))
    stats["chars"] = sum(len(item.get("text", "")) for item in result.get("items", []))
    sys.stderr.write(json.dumps(stats, sort_keys=True) + "\n")
    if result["text"]:
        sys.stdout.write(result["text"] + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
