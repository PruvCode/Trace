"""Automatic user-channel memory injection for OpenCode.

The ``experimental.chat.system.transform`` path delivers prior memory as a
system message, which the model demonstrably ignores in clean trials.
This module owns the alternate path: a clearly labeled synthetic
*user-channel* message injected via
``experimental.chat.messages.transform`` into ``output.messages``.

That hook carries no session identifier (its input is ``{}``), so the
current session must be resolved another way: the newest OpenCode session
row for this project directory. ``opencode run`` creates exactly one
session per process, so newest-for-directory *is* the current session in
normal use. Anything ambiguous fails open (inject nothing):

- no session row for this directory (hook fired before the session commit),
- the newest session is stale (continued old sessions, background servers),
- the second-newest session is still live -- its ``time_updated`` is not
  older than the newest session's creation (concurrent runs or a newer
  sibling session -- newest-for-directory no longer identifies the caller).

Note the liveness check deliberately compares against update time, not
creation time: back-to-back sequential sessions are the normal case and
must proceed (the older session ended before the newest began).

Resolution is stateless (fresh read per hook fire), so restarts need no
recovery. Retrieval reuses ``memory.session_context`` with the resolved
session excluded, preserving the bounded policy, redaction, isolation,
and fail-open behavior. No embeddings, no network, local SQLite only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from memory import session_context as session_context_mod
from memory import synthetic as synthetic_mod

# A session row newer than this is plausibly the session currently firing
# the hook. Older newest-rows (continued sessions, idle servers) and two
# fresh rows for one directory (concurrent runs) both fail open.
FRESH_WINDOW_MS = 300_000


def normalize_dir(project_dir: str | Path) -> str:
    """Normalize a project directory the way OpenCode stores it."""
    return str(Path(project_dir).resolve()).replace("\\", "/")


def resolve_current_session(
    opencode_db_path: str | Path,
    project_dir: str | Path,
) -> dict[str, Any]:
    """Map project directory to the currently active OpenCode session.

    Returns ``{"ok", "session_id", "reason"}``. ``ok`` is True only when
    the directory's newest session row is fresh and unambiguous. Never
    raises for database problems -- those fail open with ``ok=False``.
    """
    try:
        db_path = Path(opencode_db_path)
        if not db_path.exists():
            return {"ok": False, "session_id": None, "reason": "no-database"}
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT id, time_created, time_updated FROM session WHERE directory = ? "
                "ORDER BY time_created DESC, rowid DESC LIMIT 2",
                (normalize_dir(project_dir),),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {"ok": False, "session_id": None, "reason": "lookup-error"}
    if not rows:
        return {"ok": False, "session_id": None, "reason": "no-session"}
    now_ms = int(time.time() * 1000)
    newest_id, newest_tc, _newest_tu = rows[0][0], rows[0][1] or 0, rows[0][2] or 0
    if not newest_id or (now_ms - newest_tc) > FRESH_WINDOW_MS:
        return {"ok": False, "session_id": None, "reason": "stale-or-unknown"}
    if len(rows) > 1:
        second_tu = rows[1][2] or 0
        if second_tu >= newest_tc:
            # The sibling session was updated at or after the newest
            # session began: it is live (concurrent run) or newer activity
            # exists outside the firing session. Fail open.
            return {"ok": False, "session_id": None, "reason": "concurrent"}
    return {"ok": True, "session_id": newest_id, "reason": "ok"}


def get_message_start_context(
    trace_db_path: str | Path,
    *,
    opencode_db_path: str | Path,
    project_dir: str | Path,
    max_items: int = session_context_mod.MAX_ITEMS,
    max_chars: int = session_context_mod.MAX_CHARS,
) -> dict[str, Any]:
    """Assemble the user-channel memory block. Never raises for DB problems.

    Returns ``{"items", "text", "truncated", "stats"}`` where ``stats``
    carries ``resolved``/``reason``/``currentSession``. Unresolvable
    sessions yield ``items=[]`` and ``text=""``.
    """
    started = time.perf_counter()
    stats: dict[str, Any] = {
        "resolved": False,
        "reason": None,
        "currentSession": None,
        "message_items": 0,
        "finding_items": 0,
        "latency_ms": 0,
    }
    resolution = resolve_current_session(opencode_db_path, project_dir)
    stats["reason"] = resolution["reason"]
    if not resolution["ok"]:
        return {"items": [], "text": "", "truncated": False, "stats": stats}
    stats["resolved"] = True
    stats["currentSession"] = resolution["session_id"]
    result = session_context_mod.get_session_start_context(
        trace_db_path,
        exclude_session_id=resolution["session_id"],
        max_items=max_items,
        max_chars=max_chars,
    )
    stats["message_items"] = sum(1 for i in result["items"] if i.get("kind") == "message")
    stats["finding_items"] = sum(1 for i in result["items"] if i.get("kind") == "finding")
    stats["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return {
        "items": result["items"],
        "text": synthetic_mod.render_user_block(result["items"]),
        "truncated": result["truncated"],
        "stats": stats,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI: print the user-channel block (or nothing). Always fail-open."""
    parser = argparse.ArgumentParser(description="Assemble TRACE user-channel memory")
    parser.add_argument("--db", required=True, help="TRACE memory.db path")
    parser.add_argument("--opencode-db", required=True, help="OpenCode opencode.db path")
    parser.add_argument("--project-dir", required=True, help="Project directory to resolve")
    args = parser.parse_args(argv)
    try:
        result = get_message_start_context(
            args.db, opencode_db_path=args.opencode_db, project_dir=args.project_dir
        )
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
