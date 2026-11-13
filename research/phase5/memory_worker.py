"""Run the checked-out version of TRACE against one isolated replay database."""

from __future__ import annotations

import json
import re
import sys
from contextlib import closing
from pathlib import Path


def run(code_root: Path, database: Path, operation: str, args: dict) -> object:
    sys.path.insert(0, str(code_root))
    from memory import session_context, store, synthetic, transcript

    task_id = args.get("task_id")
    with closing(store.connect(database)) as db:
        if operation == "seed":
            store.init_schema(db)
            for session, stamp in (("history-1", "2026-01-01T09:59:00Z"), ("follow-up", "2026-01-06T09:00:00Z")):
                transcript.record_message(db, source_id=f"session:{session}", session_id=session, role="system",
                                          content="Session started", timestamp=stamp, metadata={"agent": "opencode"})
            if args["enhanced"]:
                from memory import captured_outcomes, task_library

                task_id = task_library.create_task(db, args["title"], "pilot")["id"]
                task_library.bind_session(db, task_id, "history-1")
                task_library.bind_session(db, task_id, "follow-up")
                policy = captured_outcomes.configure(db, "on")
                assert captured_outcomes.record(
                    db, True, project=database.parent, repo="synthetic/phase5", session_id="history-1",
                    message_id="historical-test", tool_id="pytest-1", created_ms=policy["enabled_since_ms"] + 1,
                    part=args["test_part"], v2=True,
                )
            for row in args["messages"]:
                transcript.record_message(db, **row)
            return {"task_id": task_id}
        if operation in ("context", "context_v2"):
            options = {"exclude_session_id": "follow-up", "query": args["query"],
                       "max_chars": 2000, "max_items": 5}
            if operation == "context_v2":
                options.update(task_id=args["task"], project_root=Path(args["project"]))
            result = session_context.get_session_start_context(database, **options)
            items = result["items"]
            while items and len(synthetic.render_user_block(items)) > 2000:
                items.pop()
            return {"text": synthetic.render_user_block(items),
                    "references": [ref["ref"] for item in items for ref in (item.get("evidence") or [])],
                    "latency_ms": result["stats"]["latency_ms"]}
        if operation == "search":
            tokens = list(dict.fromkeys(re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", args["query"][:96])))[:8]
            if not tokens:
                return ""
            records = transcript.search_messages(db, query=" OR ".join(f'"{token}"' for token in tokens), limit=4)
            from memory import redact
            items = [{"kind": "message", "role": rec.role, "text": redact.redact_text(rec.content)[:400]}
                     for rec in records]
            while items and len(synthetic.render_user_block(items)) > 2000:
                items.pop()
            return synthetic.render_user_block(items)
        if operation == "evidence":
            from memory import evidence

            return evidence.expand(db, None, args["reference"], task_id=task_id)
    raise ValueError("Unsupported operation")


if __name__ == "__main__":
    root, db, op = sys.argv[1:]
    args = json.load(sys.stdin)
    output = json.dumps(run(Path(root), Path(db), op, args), ensure_ascii=True)
    sys.stdout.write(output)
