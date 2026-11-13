"""Compare TRACE 2.0 with frozen controls under qualified loopback inference."""

from contextlib import closing
import argparse
import json
from pathlib import Path
import random
import re
from statistics import median
from tempfile import TemporaryDirectory

from memory import episodic, session_context, store, synthetic, task_library, transcript
from research.preview import effectiveness as harness
from research.preview.coding import CASES, QUALIFY, SEED
from research.phase5.grader import Grader
from research.phase5.pilot import history_rows, memory_call
from research.production.multi_file import Case
from trace_memory import project

ARMS = ("no_memory", "notes", "previous_trace", "trace_2")
QWEN = "qwen2.5-coder:7b"
DIGEST = "dae161e27b0e90dd1856c8bb3209201fd6736d8eb66298e75ed87571486f4364"

HELD = (
    Case("refresh", "Update refresh in handler.py and delay in protocol.py using the approved retry decision.",
         "For retry, delay(attempt) is 3 for attempts 0 and 1, and 9 for attempt 2 or greater. "
         "refresh(attempt, allowed) returns None when not allowed or attempt is negative. Otherwise return "
         "{'retry_in': delay(attempt), 'attempt': attempt}.",
         ("def delay(attempt):\n    return 2", "from protocol import delay\ndef refresh(attempt, allowed):\n    return delay(attempt)"),
         ("def delay(attempt):\n    return 3 if attempt < 2 else 9",
          "from protocol import delay\ndef refresh(attempt, allowed):\n    if not allowed or attempt < 0:\n"
          "        return None\n    return {'retry_in': delay(attempt), 'attempt': attempt}"),
         (("protocol.delay(0)", 3), ("protocol.delay(2)", 9), ("handler.refresh(-1, True)", None),
          ("handler.refresh(2, False)", None), ("handler.refresh(1, True)", {"retry_in": 3, "attempt": 1}),
          ("handler.refresh(2, True)", {"retry_in": 9, "attempt": 2}))),
    Case("quote", "Update units in protocol.py and quote in handler.py to meet the approved shipping decision.",
         "For shipping, units(weight) accepts only positive integer weights and returns None otherwise. "
         "For valid weight, return the count of started groups of four units. quote(weight) returns None for "
         "invalid weight. Otherwise return {'groups': units(weight), 'fee': 11 * units(weight)}.",
         ("def units(weight):\n    return weight // 4",
          "from protocol import units\ndef quote(weight):\n    return units(weight) * 6"),
         ("def units(weight):\n    if type(weight) is not int or weight <= 0:\n        return None\n    return (weight + 3) // 4",
          "from protocol import units\ndef quote(weight):\n    size = units(weight)\n"
          "    return None if size is None else {'groups': size, 'fee': 11 * size}"),
         (("protocol.units(4)", 1), ("protocol.units(5)", 2), ("protocol.units(True)", None),
          ("handler.quote(0)", None), ("handler.quote('5')", None), ("handler.quote(9)", {"groups": 3, "fee": 33}))),
    Case("audit", "Apply the approved retention decision to retention in protocol.py and audit in handler.py.",
         "For retention, retention(level) ignores case and surrounding spaces, returning 17 for 'trace', "
         "31 for 'audit', and None otherwise. audit(level, enabled) returns None when disabled or "
         "unrecognized, else {'days': retention(level), 'active': True}.",
         ("def retention(level):\n    return 14",
          "from protocol import retention\ndef audit(level, enabled):\n    return [retention(level), enabled]"),
         ("def retention(level):\n    return {'trace': 17, 'audit': 31}.get(level.strip().lower())",
          "from protocol import retention\ndef audit(level, enabled):\n    days = retention(level)\n"
          "    return {'days': days, 'active': True} if enabled and days is not None else None"),
         (("protocol.retention(' TRACE ')", 17), ("protocol.retention('Audit')", 31),
          ("handler.audit('other', True)", None), ("handler.audit('trace', False)", None),
          ("handler.audit(' AUDIT ', True)", {"days": 31, "active": True}))),
)


def snapshot(room, case):
    room.mkdir()
    for name, source in zip(harness.FILES, case.original):
        (room / name).write_text(source + "\n", encoding="utf-8")


def project_block(room, case, *, contested):
    project.init_project(room)
    db = project.db_path(room)
    with closing(store.connect(db)) as conn:
        task = task_library.create_task(conn, case.name, "fixture")["id"]
        transcript.record_message(conn, session_id="history-1", role="system", source_id="session:history-1",
                                  content="Session started")
        task_library.bind_session(conn, task, "history-1")
        symbol = re.search(r"def ([A-Za-z_]+)\(", case.original[1])[1]
        old_text = f"{case.name} obsolete proposal. Keep the previous implementation. SUPERSEDED_FIXTURE"
        rows = history_rows(case, "Reviewed both files. No implementation changes.")
        if contested:
            rows.insert(0, {"session_id": "history-1", "role": "user", "content": old_text,
                            "timestamp": "2026-01-01T09:50:00Z"})
        decision_id = old_id = None
        for row in rows:
            message_id = transcript.record_message(conn, **row)
            if row["content"] == case.review:
                decision_id = message_id
            elif row["content"] == old_text:
                old_id = message_id
        assert decision_id is not None
        old = episodic.record_event(conn, repo="fixture", type="investigation", file="handler.py", symbol=symbol,
                                    task_id=task, payload={"summary": old_text, "status": "hypothesis"},
                                    evidence=[{"type": "transcript", "message_id": old_id}] if old_id else None)
        new = episodic.record_event(conn, repo="fixture", type="decision", file="handler.py", symbol=symbol,
                                    task_id=task, payload={"decision": case.review, "status": "verified", "supersedes": old},
                                    evidence=[{"type": "transcript", "message_id": decision_id}])
        if contested:
            for i in range(75):
                episodic.record_event(conn, repo="fixture", type="observation", task_id=task,
                                      payload={"summary": f"{case.name} routine pending check {i}"})
    options = {"query": case.task, "task_id": task, "max_chars": 2000, "max_items": 5, "project_root": room}
    return task, new, options, db


def prepare(case, arm, room, previous, contested):
    snapshot(room, case)
    if arm == "no_memory":
        return "", 0, None
    if arm == "notes":
        return "<project-notes>\n" + json.dumps({"text": case.review}) + "\n</project-notes>", 0, None
    task, event, options, db = project_block(room, case, contested=contested)
    if arm == "previous_trace":
        result = memory_call(previous, db, "context_v2", task=task, query=case.task, project=str(room))
        return result["text"], len(result["references"]), result["latency_ms"]
    result = session_context.get_session_start_context(db, **options)
    assert any(item.get("event_id") == event for item in result["items"])
    refs = sum(len(item.get("evidence") or ()) for item in result["items"])
    return synthetic.render_user_block(result["items"]), refs, result["stats"]["latency_ms"]


def run(args, room):
    cases = tuple(case for case in (*CASES, *HELD) if not args.tasks or case.name in args.tasks)
    grader = Grader(args.wasi)
    canary = room / "canary"
    canary.write_text("synthetic filesystem boundary", encoding="utf-8")
    grader.verify_boundary(canary)
    for case in (*QUALIFY, *cases):
        assert not harness.evaluate(grader, case.original, case)
        assert harness.evaluate(grader, case.reference, case)
    harness.MODEL, harness.DIGEST = (harness.MODEL, harness.DIGEST) if args.gemma else (QWEN, DIGEST)
    harness.MAX_REQUESTS = len(QUALIFY) + (0 if args.qualify_only else len(cases) * len(ARMS) * args.repeats)
    local = None if args.context_only else harness.LocalRun()
    if local:
        qualified = 0
        for case in QUALIFY:
            row = local.grade(grader, case, case.review)
            qualified += row["passed"]
            harness.emit("qualification", task=case.name, **row)
        if qualified != len(QUALIFY):
            harness.emit("summary", qualified=qualified, scored=0, requests=local.requests)
            return
        if args.qualify_only:
            harness.emit("summary", qualified=qualified, scored=0, requests=local.requests)
            return
    rng = random.Random(SEED)
    scored = {arm: [] for arm in ARMS}
    for repeat in range(args.repeats):
        for case in cases:
            for arm in rng.sample(ARMS, len(ARMS)):
                block, refs, retrieval = prepare(case, arm, room / f"{repeat}-{case.name}-{arm}",
                                                 args.previous, case in HELD)
                assert len(block) <= 2000
                recall = case.review in block
                row = {"task": case.name, "arm": arm, "repeat": repeat, "contested": case in HELD,
                       "exact_decision": recall, "evidence_references": refs, "retrieval_ms": retrieval,
                       "superseded_text_present": "SUPERSEDED_FIXTURE" in block}
                if local:
                    row.update(local.grade(grader, case, block))
                scored[arm].append(row)
                harness.emit("scored" if local else "replay", **row)
    summary = {}
    for arm, rows in scored.items():
        result = {"tasks": len(rows), "exact_decisions": sum(row["exact_decision"] for row in rows),
                  "superseded_injections": sum(row["superseded_text_present"] for row in rows)}
        if local:
            result.update(passed=sum(row["passed"] for row in rows),
                          median_input_tokens=median(row["input_tokens"] for row in rows),
                          median_output_tokens=median(row["output_tokens"] for row in rows),
                          median_model_ms=median(row["elapsed_ms"] for row in rows))
        summary[arm] = result
    harness.emit("summary", qualified=2 if local else None, arms=summary, requests=local.requests if local else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--wasi", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--context-only", action="store_true")
    parser.add_argument("--gemma", action="store_true")
    parser.add_argument("--qualify-only", action="store_true")
    parser.add_argument("--repeats", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--tasks", nargs="+", choices=[case.name for case in (*CASES, *HELD)])
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run with assertions enabled.")
    if args.context_only and args.qualify_only:
        parser.error("--qualify-only needs loopback inference")
    args.previous = args.previous.resolve(strict=True)
    with TemporaryDirectory(prefix="trace2-eval-", dir=args.scratch) as room:
        run(args, Path(room).resolve())


if __name__ == "__main__":
    main()
