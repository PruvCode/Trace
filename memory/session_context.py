"""Bounded session-start context assembly for automatic retrieval.

At session start there is no user query yet, so retrieval is recency-based:
the newest conversation of prior sessions plus recent recorded findings,
read from the current project's TRACE database only (isolation by
construction — no other project's database is ever opened).

Policy:
- the newest 10 user/assistant messages, taken from the most recent prior
  session first and from older sessions only while that budget is unused;
  status rows (step-finish "tool-calls"/"stop", step markers) never count
- subagent (child) sessions and the current session are skipped
- at most 5 recent findings recorded by agents/people (auto-captured tool
  activity is searchable but never presented as a finding)
- at most 20 items and 4000 characters; when over budget the oldest
  messages yield first, and up to FINDINGS_RESERVE characters stay
  reserved for findings
- long messages keep their head and tail (conclusions are often last);
  assistant messages up to 1500 characters, others up to 500
- malformed rows skipped; any retrieval error fails open (empty result)
- secret-like values redacted with the shared capture-side rules
- no embeddings, no network, local SQLite only
"""

from __future__ import annotations

import argparse
import re
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory import episodic as episodic_mod
from memory import redact as redact_mod
from memory import store as store_mod
from memory import synthetic as synthetic_mod
from memory import transcript as transcript_mod

MAX_TAIL_MESSAGES = 10
MAX_FINDINGS = 5
MAX_ITEMS = 20
MAX_CHARS = 4000
MAX_ITEM_CHARS = 500
# Assistant messages carry the conclusions (often a long final summary); a
# 500-char cap cut a real 1.1k-char summary mid-fact in live testing.
MAX_ASSISTANT_ITEM_CHARS = 1500
FINDINGS_RESERVE = 1200

# Payload keys that carry the human-readable part of a recorded event.
_FINDING_KEYS = ("finding", "decision", "message", "content", "action")
_FINDING_DETAIL_KEYS = ("detail", "result", "rationale")


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """Keep head and tail of an over-long item; conclusions are often last."""
    if len(text) <= limit:
        return text, False
    head = limit * 2 // 5
    tail = limit - head - 3
    return text[:head] + " … " + text[-tail:], True


def _tail_messages(
    conn: sqlite3.Connection,
    exclude_session_id: str | None,
    limit: int = MAX_TAIL_MESSAGES,
) -> tuple[list[dict], list[str]]:
    """Newest conversational messages across prior sessions, oldest first.

    The most recent prior session is read first; older sessions only fill
    budget it leaves unused, so a long last session is never diluted with
    older, possibly stale, work.
    """
    try:
        sessions = transcript_mod.list_sessions(conn, limit=50)
    except (sqlite3.Error, ValueError):
        return [], []
    groups: list[list[dict]] = []
    remaining = limit
    for entry in sessions:
        if remaining <= 0:
            break
        try:
            session_id = entry.get("session_id")
            when = entry.get("last_activity")
        except AttributeError:
            continue
        if not session_id or session_id == exclude_session_id:
            continue
        try:
            if transcript_mod.session_parent(conn, session_id):
                continue  # subagent transcript, not a user-facing session
            messages = transcript_mod.get_recent_conversation(conn, session_id, remaining)
        except (sqlite3.Error, ValueError):
            continue
        group: list[dict] = []
        for msg in messages:
            content = (msg.content or "").strip()
            if not content or synthetic_mod.is_synthetic_memory_text(content):
                continue
            cap = MAX_ASSISTANT_ITEM_CHARS if msg.role == "assistant" else MAX_ITEM_CHARS
            # Redact first: truncation can cut a secret out of its pattern.
            text, _ = _truncate(redact_mod.redact_text(content), cap)
            group.append({
                "kind": "message",
                "role": msg.role,
                "text": text,
                "session": session_id,
                "when": when,
            })
        if group:
            groups.append(group)
            remaining -= len(group)
    ordered = [item for group in reversed(groups) for item in group]
    return ordered, [group[0]["session"] for group in groups]


_STOP = frozenset("""about after again also before being could does done each from have into just like make more most
must need only other over same should some such than that their them then there these they this those through under
until very want were what when where which while will with without would your code file files change changes""".split())


def _query_terms(text: str, limit: int = 12) -> str | None:
    """Longest distinctive words of the task prompt, OR-joined for FTS."""
    words = dict.fromkeys(w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text or ""))
    terms = [w for w in words if w not in _STOP][:limit]
    return " OR ".join(terms) if terms else None


def _relevant_messages(conn: sqlite3.Connection, query: str, exclude_session_id: str | None,
                       limit: int = MAX_TAIL_MESSAGES) -> list[dict]:
    """Prior-session messages ranked by match with the current task (best first)."""
    terms = _query_terms(query)
    if not terms:
        return []
    try:
        hits = transcript_mod.search_messages(conn, query=terms, limit=limit * 3)
    except (sqlite3.Error, ValueError, store_mod.SearchQueryError):
        return []
    out: list[dict] = []
    for msg in hits:
        content = (msg.content or "").strip()
        if (msg.session_id == exclude_session_id or not content
                or msg.role not in ("user", "assistant")
                or synthetic_mod.is_synthetic_memory_text(content)
                or transcript_mod.session_parent(conn, msg.session_id)):
            continue
        cap = MAX_ASSISTANT_ITEM_CHARS if msg.role == "assistant" else MAX_ITEM_CHARS
        out.append({"kind": "message", "role": msg.role, "text": _truncate(redact_mod.redact_text(content), cap)[0],
                    "session": msg.session_id, "when": msg.timestamp})
        if len(out) >= limit:
            break
    return out


def _is_captured_tool_activity(data: object) -> bool:
    """Auto-captured tool-call payloads, including legacy rows stored before source="capture"."""
    return isinstance(data, dict) and "tool" in data and "input" in data and (
        "status" in data or "tool_status" in data)


def _finding_text(payload: object) -> str:
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except (ValueError, TypeError):
        return str(payload or "")
    if not isinstance(data, dict):
        return str(payload or "")
    main = next((str(data[k]) for k in _FINDING_KEYS if data.get(k)), "")
    extra = [str(data[k]) for k in _FINDING_DETAIL_KEYS if data.get(k)]
    if not main and not extra:
        return json.dumps(data, sort_keys=True)
    return " — ".join([main, *extra] if main else extra)


def _staleness(project_root: Path | None, file: str | None, recorded: str | None) -> str | None:
    """Label a file-linked finding whose file changed after it was recorded.

    Only the finding's own file is checked, so unrelated edits or commits
    never mark it. Modification time is a conservative signal: a checkout
    that rewrites the file also triggers the warning. Findings without a
    file link make no staleness claim.
    """
    if project_root is None or not file:
        return None
    path = (project_root / file).resolve()
    if not path.is_relative_to(project_root.resolve()):
        return None
    if not path.exists():
        return f"may be stale: {file} no longer exists"
    try:
        when = datetime.fromisoformat(recorded) if recorded else None
    except ValueError:
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if path.stat().st_mtime > when.timestamp() + 1:
        return f"may be stale: {file} changed after this was recorded"
    return None


def _recent_findings(conn: sqlite3.Connection, project_root: Path | None = None) -> list[dict]:
    """The most recent current findings (agent/person/git), chronological.

    Superseded findings are left out (they stay searchable with
    ``superseded_by``); hypotheses and verified findings are labeled, and a
    finding whose linked file changed since it was recorded is flagged.
    """
    try:
        superseded = episodic_mod.superseded_map(conn)
        rows = conn.execute(
            "SELECT id, type, payload, file, timestamp FROM events WHERE source <> 'capture'"
            " ORDER BY id DESC LIMIT ?",
            (MAX_FINDINGS * 5 + len(superseded),),
        ).fetchall()
    except sqlite3.Error:
        return []
    items: list[dict] = []
    for row in rows:
        if len(items) >= MAX_FINDINGS:
            break
        try:
            if row[0] in superseded:
                continue
            event_type = row[1] or "event"
            payload = row[2] or ""
            try:
                data = json.loads(payload)
            except (ValueError, TypeError):
                data = None
            if _is_captured_tool_activity(data):
                continue
            text = _finding_text(payload).strip()
            if not text:
                continue
            labels = []
            status = data.get("status") if isinstance(data, dict) else None
            if status == "hypothesis":
                labels.append("hypothesis, unverified")
            elif status == "verified":
                labels.append("verified")
            stale = _staleness(project_root, row[3], row[4])
            if stale:
                labels.append(stale)
            text, _ = _truncate(redact_mod.redact_text(text), MAX_ITEM_CHARS)
            items.append({
                "kind": "finding",
                "role": f"{event_type} ({'; '.join(labels)})" if labels else str(event_type),
                "text": text,
            })
        except (IndexError, TypeError, AttributeError, OSError):
            continue
    items.reverse()  # chronological within the block
    return items


def _apply_budgets(items: list[dict], max_items: int = MAX_ITEMS, max_chars: int = MAX_CHARS) -> tuple[list[dict], bool]:
    """Enforce item/character budgets on an already-prioritized list.

    Items are kept in order until a budget is hit; the rest are dropped
    whole (never re-sliced mid-item).
    """
    kept: list[dict] = []
    total = 0
    for item in items:
        if len(kept) >= max(0, max_items) or total + len(item["text"]) > max(0, max_chars):
            return kept, True
        kept.append(item)
        total += len(item["text"])
    return kept, False


def _select(messages: list[dict], findings: list[dict], max_items: int, max_chars: int) -> tuple[list[dict], bool]:
    """Fit messages and findings into the budgets; the oldest messages yield first."""
    reserve = min(sum(len(f["text"]) for f in findings), FINDINGS_RESERVE)
    msg_kept, msg_cut = _apply_budgets(
        list(reversed(messages)), max_items - min(len(findings), max_items), max_chars - reserve
    )
    used = sum(len(m["text"]) for m in msg_kept)
    find_kept, find_cut = _apply_budgets(
        list(reversed(findings)), max_items - len(msg_kept), max_chars - used
    )
    return list(reversed(msg_kept)) + list(reversed(find_kept)), msg_cut or find_cut


def format_block(items: list[dict]) -> str:
    """Render items as a compact human-readable context block."""
    if not items:
        return ""
    return "\n".join(["<TRACE MEMORY>", *synthetic_mod.render_records(items), "</TRACE MEMORY>"])


def get_session_start_context(
    trace_db_path: str | Path,
    *,
    exclude_session_id: str | None = None,
    max_items: int = MAX_ITEMS,
    max_chars: int = MAX_CHARS,
    query: str | None = None,
) -> dict[str, Any]:
    """Assemble bounded prior-session context. Never raises for DB problems.

    With ``query`` (the new session's task prompt), prior messages that match
    it are kept ahead of the plain recency tail.

    Returns {"items", "text", "truncated", "stats"}. Empty memory, missing
    DB, or any retrieval error yields items=[] and text="".
    """
    started = time.perf_counter()
    stats: dict[str, Any] = {
        "prior_sessions": [],
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
            messages, prior_sessions = _tail_messages(conn, exclude_session_id)
            if query:
                seen = {m["text"] for m in messages}
                relevant = [m for m in _relevant_messages(conn, query, exclude_session_id)
                            if m["text"] not in seen]
                # _select keeps the END of the list first, so relevant hits win.
                messages = messages + list(reversed(relevant))
                stats["relevant_items"] = len(relevant)
            # Staleness checks need the project tree: <project>/.agent-memory/memory.db
            root = db_path.resolve().parent.parent if db_path.parent.name == ".agent-memory" else None
            findings = _recent_findings(conn, root)
        finally:
            conn.close()
    except Exception:
        return {"items": [], "text": "", "truncated": False, "stats": stats}

    items, truncated = _select(messages, findings, max_items, max_chars)
    # The limit applies to what the model sees, labels and headers included.
    while items and len("\n".join(synthetic_mod.render_lines(items))) > max_chars:
        oldest_message = next((i for i in items if i["kind"] == "message"), None)
        items.remove(oldest_message if oldest_message is not None else items[-1])
        truncated = True
    stats["prior_sessions"] = prior_sessions
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
    sys.stdout.reconfigure(encoding="utf-8")  # see memory.message_context.main
    sys.stderr.reconfigure(encoding="utf-8")
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
