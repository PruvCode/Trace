"""Bounded session-start context assembly for automatic retrieval.

Retrieval reads the current project's TRACE database only (isolation by
construction — no other project's database is ever opened). When the new
session's first prompt matches earlier messages, those matches lead the
block and only a short tail of the last session stays, within a smaller
budget, because the block repeats on every model request. Otherwise
retrieval falls back to recency: the newest conversation of prior sessions
plus recent recorded findings.

Recency policy:
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

from memory import captured_outcomes
from memory import episodic as episodic_mod
from memory import evidence as evidence_mod
from memory import redact as redact_mod
from memory import relevance
from memory import store as store_mod
from memory import synthetic as synthetic_mod
from memory import task_library
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
# With task matches: the block is resent on every request, so stay small.
MAX_RELEVANT = 6
MAX_TASK_TAIL = 2
MAX_TASK_CHARS = 2000

# Payload keys that carry the human-readable part of a recorded event.
_FINDING_KEYS = ("finding", "decision", "summary", "message", "content", "action")
_FINDING_DETAIL_KEYS = ("detail", "result", "rationale")


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """Keep head and tail of an over-long item; conclusions are often last."""
    limit = max(0, limit)
    if len(text) <= limit:
        return text, False
    if limit <= 5:
        return text[:limit], True
    head = limit * 2 // 5
    tail = limit - head - 3
    return text[:head] + " … " + text[-tail:], True


def _tail_messages(
    conn: sqlite3.Connection,
    exclude_session_id: str | None,
    limit: int = MAX_TAIL_MESSAGES,
    *,
    retired: list[int] | None = None,
) -> tuple[list[dict], list[str]]:
    """Newest conversational messages across prior sessions, oldest first.

    The most recent prior session is read first; older sessions only fill
    budget it leaves unused, so a long last session is never diluted with
    older, possibly stale, work.
    """
    try:
        sessions = transcript_mod.list_sessions(conn, limit=50, unassigned_only=True)
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
            messages = transcript_mod.get_recent_conversation(conn, session_id, remaining, exclude_superseded=retired)
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


# Stated constraints and specifics are what a later session cannot rediscover.
_SPECIFIC = re.compile(
    r"\b(?:must|never|always|do not|don't|because|decision|decided|requires?|only|until|breaks?|caused|capped)\b|\d", re.I)
_DECISION = re.compile(r"\b(?:" + "|".join(transcript_mod.DECISION_WORDS) + r")\b", re.I)


def _query_terms(text: str, limit: int = 16) -> list[str]:
    """Distinctive words of the task prompt, with word endings dropped for matching."""
    words = dict.fromkeys(w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text or ""))
    return [w[:-2] if len(w) >= 6 else w for w in words if w not in _STOP][:limit]


def _relevant_messages(conn: sqlite3.Connection, query: str, exclude_session_id: str | None,
                       limit: int = MAX_RELEVANT, *, task_id: str | None = None,
                       retired: list[int] | None = None) -> list[dict]:
    """Prior-session messages ranked by match with the current task (best first).

    Messages sharing more of the task's terms come first, a stated constraint
    or specific value counts as one more, and BM25 breaks ties.
    """
    terms = _query_terms(query)
    if not terms:
        return []
    prefixes = [f"{term}*" for term in terms]
    scope = {"unassigned_only": task_id is None, "task_id": task_id, "exclude_session_id": exclude_session_id}
    if retired:
        scope["exclude_superseded"] = retired
    try:
        hits = transcript_mod.search_messages(conn, query=" OR ".join(prefixes), limit=store_mod.MAX_LIMIT, **scope)
        if len(prefixes) > 1 and len(hits) == store_mod.MAX_LIMIT:
            # Preserve a matching explicit decision when short routine messages fill the FTS shortlist.
            decisions = transcript_mod.search_messages(
                conn, query=" ".join(prefixes), limit=limit, decision_only=True, **scope,
            )
            hits = list({message.id: message for message in [*decisions, *hits]}.values())
    except (sqlite3.Error, ValueError, store_mod.SearchQueryError):
        return []

    def score(msg: transcript_mod.TranscriptMessage) -> int:
        content = msg.content or ""
        words = re.findall(r"[a-z0-9_]+", content.lower())
        return sum(any(w.startswith(t) for w in words) for t in terms) + bool(
            _SPECIFIC.search(content) or _DECISION.search(content)
        )

    out: list[dict] = []
    for msg in sorted(hits, key=score, reverse=True):
        content = (msg.content or "").strip()
        if (msg.session_id == exclude_session_id or not content
                or msg.role not in ("user", "assistant")
                or msg.metadata.get("incomplete")
                or msg.metadata.get("part_type") in transcript_mod.NON_CONVERSATIONAL_PART_TYPES
                or synthetic_mod.is_synthetic_memory_text(content)
                or transcript_mod.session_parent(conn, msg.session_id)):
            continue
        cap = MAX_ASSISTANT_ITEM_CHARS if msg.role == "assistant" and task_id is None else MAX_ITEM_CHARS
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
        details = {key: value for key, value in data.items() if key != "acceptance"}
        return json.dumps(details, sort_keys=True) if details else ""
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


def _recent_findings(conn: sqlite3.Connection, project_root: Path | None = None, *, retired=()) -> list[dict]:
    """The most recent current findings (agent/person/git), chronological.

    Superseded findings are left out (they stay searchable with
    ``superseded_by``); hypotheses and verified findings are labeled, and a
    finding whose linked file changed since it was recorded is flagged.
    """
    try:
        # Unbound sessions only receive general findings.
        scope = " AND task_id IS NULL" if store_mod.has_task_schema(conn) else ""
        rows = conn.execute(
            "SELECT id, type, payload, file, timestamp FROM events WHERE source <> 'capture'"
            f"{scope} AND id NOT IN (SELECT value FROM json_each(?)) ORDER BY id DESC LIMIT ?",
            (json.dumps(list(retired)), MAX_FINDINGS * 5),
        ).fetchall()
    except sqlite3.Error:
        return []
    items: list[dict] = []
    for row in rows:
        if len(items) >= MAX_FINDINGS:
            break
        try:
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


def _scoped_findings(conn, project, task_id, query, *, exact_only=False, file=None, retired=()) -> list[dict]:
    terms = _query_terms(query or "")
    match = " OR ".join(f"{term}*" for term in terms)
    exact = relevance.graph_matches(conn, query, task_id, unassigned_only=task_id is None, exclude=retired)
    if task_id is None and not match and not exact:
        return []
    options = {"task_id": task_id, "unassigned_only": task_id is None, "limit": 25, "file": file, "normalize_file": True}
    hits = episodic_mod.search_events(conn, query=match, **options) if match and not exact_only else []
    matched = {event.id for event in hits}
    candidates = episodic_mod.search_events(conn, ids=list(exact), **{**options, "limit": 30}) if exact else []
    if match and not exact_only:
        candidates += episodic_mod.search_events(conn, query=match, decision_only=True, **options)
        candidates += hits
    if task_id is not None and not exact_only:
        candidates += episodic_mod.search_events(conn, newest_first=True, **options)
        if file is None:
            candidates += episodic_mod.search_events(conn, task_id=task_id, newest_first=True,
                                                     captured_failure_only=True, limit=3)
    selected = {}
    for event in candidates:
        if event.superseded_by is not None or not isinstance(event.payload, dict):
            continue
        if event.source == "capture":
            if task_id is None or event.payload.get("kind") != "test_outcome" or not event.evidence:
                continue
        elif _is_captured_tool_activity(event.payload):
            continue
        if _finding_text(event.payload).strip():
            selected.setdefault(event.id, event)

    keys = []
    priorities = {}
    seen = set()
    for event in sorted(selected.values(), key=lambda item: item.id, reverse=True):
        keys.extend((link["commit"], link["path"]) for link in event.evidence[:2] if link["type"] == "git")
        if event.commit and event.file:
            keys.append((event.commit, event.file))
    objects = evidence_mod.git_objects(project, keys)
    checked = {key: evidence_mod.freshness(project, key[1], value) for key, value in objects.items()}
    items = []
    supported_decisions = set()
    for event in selected.values():
        refs = []
        states = []
        for ref, link in zip(event.evidence_refs[:2], event.evidence[:2]):
            state = checked.get((link.get("commit"), link.get("path")), "unknown")
            states.append(state)
            refs.append({"ref": ref, "type": link["type"], "freshness": state})
        if len(event.evidence) > len(refs):
            states.append("unknown")
        if event.commit and event.file:
            states.append(checked.get((event.commit, event.file), "unknown"))
        else:
            try:
                if _staleness(project, event.file, event.timestamp):
                    states.append("stale")
            except (OSError, RuntimeError, ValueError):
                pass
        freshness = "stale" if "stale" in states else ("current" if states and set(states) == {"current"} else "unknown")
        status = event.payload.get("status")
        text = _truncate(redact_mod.redact_text(_finding_text(event.payload).strip()), MAX_ITEM_CHARS)[0]
        checks = []
        supported_decision = (event.type == "decision" and status == "verified" and refs and freshness != "stale")
        if supported_decision:
            try:
                checks = episodic_mod.acceptance_checks(event.payload)
            except ValueError:
                pass
        failed = captured_outcomes.failure_nodes(event.payload) if event.source == "capture" else []
        dedupe = (event.type, event.file, event.symbol, status, freshness, text, tuple(checks), tuple(failed))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        label = "test outcome" if event.source == "capture" else event.type
        if status in ("hypothesis", "observed", "verified"):
            label = f"{label} ({status})"
        if task_id is None:
            label = f"{label} (unassigned fallback)"
        item = {
            "kind": "finding", "role": label, "event_id": event.id, "task_id": event.task_id,
            "freshness": freshness, "evidence": refs or None,
            "text": text, "uncertain": status == "hypothesis" or freshness == "stale",
        }
        if event.id in exact:
            item["code"] = exact[event.id]
        if event.source != "capture":
            item["categories"] = relevance.classify(event, text)
        if status == "verified" and not refs:
            item["source_evidence"] = "unavailable"
        if checks:
            item["acceptance"] = checks
        if supported_decision and (task_id is not None or event.id in exact):
            supported_decisions.add(event.id)
        if failed:
            item["previously_failed_checks"] = failed
        priorities[event.id] = relevance.priority(event, exact=event.id in exact, matched=event.id in matched,
                                                   freshness=freshness, has_evidence=bool(refs))
        items.append(item)
    items.sort(key=lambda item: priorities[item["event_id"]])
    # Once exact source evidence answers the task, generic status updates add
    # noise. Keep linked attempts, contradictory history and other sourced work.
    if any(item.get("code") and item["event_id"] in supported_decisions for item in items):
        items = [item for item in items
                 if item["evidence"] or (item.get("code") and selected[item["event_id"]].type != "observation")]
    shortlisted = items[:MAX_FINDINGS]
    if not any(item.get("previously_failed_checks") for item in shortlisted):
        failure = next((item for item in items[MAX_FINDINGS:] if item.get("previously_failed_checks")), None)
        if failure:
            shortlisted[-1] = failure
    verified_files = {}
    for item in shortlisted:
        if item["event_id"] not in supported_decisions:
            continue
        event = selected[item["event_id"]]
        targets = relevance.decision_targets(conn, project, item["text"], event.file, event.symbol, verified_files)
        if targets:
            requirements = relevance.target_requirements(item["text"], targets)
            for target in targets:
                if rule := requirements.get(target["symbol"]):
                    target["decision_excerpt"] = rule
            item["code_targets"] = targets
    return shortlisted


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
    task_id: str | None = None,
    use_bound_task: bool = True,
    project_root: Path | None = None,
    file: str | None = None,
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
            # Staleness checks need the project tree: <project>/.agent-memory/memory.db
            root = project_root or (db_path.resolve().parent.parent if db_path.parent.name == ".agent-memory" else None)
            task_id = task_id or (task_library.bound_task_id(conn, exclude_session_id) if use_bound_task else None)
            retired = list(episodic_mod.superseded_map(conn))
            if file is not None and task_id is None:
                return {"items": [], "text": "", "truncated": False, "stats": stats}
            if task_id is not None:
                store_mod.ensure_evidence_schema(conn)
                max_chars = min(max_chars, MAX_TASK_CHARS)
                prior_sessions = []
                findings = _scoped_findings(conn, root, task_id, query, file=file, retired=retired)
                messages = (_relevant_messages(conn, query, exclude_session_id, limit=2, task_id=task_id, retired=retired)
                            if query and file is None else [])
                supported = {item["text"] for item in findings if item.get("evidence")}
                messages = [item for item in messages if item["text"] not in supported]
                if file is None and not findings and not messages:
                    findings = _scoped_findings(conn, root, None, query, retired=retired)
                    if not findings and query:
                        messages = _relevant_messages(conn, query, exclude_session_id, limit=2, retired=retired)
                    stats["task_fallback"] = bool(findings or messages)
            else:
                relevant = _relevant_messages(conn, query, exclude_session_id, retired=retired) if query else []
                exact = _scoped_findings(conn, root, None, query, exact_only=True, retired=retired) if query else []
                if exact:
                    findings = list(reversed(exact))
                    supported = {item["text"] for item in exact if item.get("evidence")}
                    messages = list(reversed([item for item in relevant if item["text"] not in supported][:2]))
                    prior_sessions = list(dict.fromkeys(item["session"] for item in messages))
                    max_chars = min(max_chars, MAX_TASK_CHARS)
                else:
                    findings = _recent_findings(conn, root, retired=retired)
                    tail_limit = MAX_TASK_TAIL if relevant else MAX_TAIL_MESSAGES
                    messages, prior_sessions = _tail_messages(conn, exclude_session_id, tail_limit, retired=retired)
                    if relevant:
                        max_chars = min(max_chars, MAX_TASK_CHARS)
                        seen = {m["text"] for m in messages}
                        relevant = [m for m in relevant if m["text"] not in seen]
                        # _select keeps the END of the list first, so relevant hits win.
                        messages += list(reversed(relevant))
                        stats["relevant_items"] = len(relevant)
        finally:
            conn.close()
    except Exception:
        return {"items": [], "text": "", "truncated": False, "stats": stats}

    try:
        if task_id is not None:
            items, truncated = [], False
            # Give a decision and the most relevant concrete failure room ahead of transcript excerpts.
            failure = next((item for item in findings[1:] if item.get("previously_failed_checks")), None)
            priority = [failure] if failure else []
            for item in [*findings[:1], *priority, *messages, *(item for item in findings[1:] if item is not failure)]:
                if item.get("code_targets") and len(synthetic_mod.render_user_block([*items, item])) > max_chars:
                    item = {key: value for key, value in item.items() if key != "code_targets"}
                    truncated = True
                if (item.get("acceptance") and len(synthetic_mod.render_user_block([*items, item])) > max_chars):
                    item = {key: value for key, value in item.items() if key != "acceptance"}
                    truncated = True
                if len(items) < max_items and len(synthetic_mod.render_user_block([*items, item])) <= max_chars:
                    items.append(item)
                else:
                    truncated = True
        else:
            items, truncated = _select(messages, findings, max_items, max_chars)
        # Oldest first, so the newest session is the one labeled "previous".
        items.sort(key=lambda i: (i["kind"] != "message", str(i.get("when") or "")))
        while items and len(synthetic_mod.render_user_block(items)) > max_chars:
            oldest_message = next((i for i in items if i["kind"] == "message"), None) if task_id is None else None
            items.remove(oldest_message if oldest_message is not None else items[-1])
            truncated = True
        if task_id is not None:
            prior_sessions = list(dict.fromkeys(i["session"] for i in items if i["kind"] == "message"))
        text = format_block(items)
    except Exception:  # noqa: BLE001 - one bad row must not break the agent's session
        return {"items": [], "text": "", "truncated": False, "stats": stats}
    stats["prior_sessions"] = prior_sessions
    stats["message_items"] = sum(1 for i in items if i["kind"] == "message")
    stats["finding_items"] = sum(1 for i in items if i["kind"] == "finding")
    stats["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return {
        "items": items,
        "text": text,
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
