"""Complete the sealed Phase 5 comparison after a disjoint model qualification."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import random
import re
import statistics
import tempfile
from pathlib import Path
from time import perf_counter

from research.phase5.comparison_cases import HELD, QUALIFICATION
from research.phase5.grader import Grader, MAX_SOURCE
from research.phase5.pilot import (
    AUDIT, DIGEST, MODEL, ROOT, endpoint, history_rows, memory_call, prepare_arm, preflight,
)

SEED = 5041
MAX_REQUESTS = len(HELD) * 6
ARMS = ("no_memory", "prompted_notes", "audited_trace", "enhanced_trace")


def decode_code(text):
    match = re.fullmatch(r"\s*\x60\x60\x60(?:python|py)?\s*\n(.*?)\n\s*\x60\x60\x60\s*", text, re.DOTALL)
    if match:
        text = match[1]
    try:
        ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    return text.strip() if 0 < len(text.strip()) <= MAX_SOURCE else None


class Local:
    def __init__(self, model, digest, cap):
        tags = endpoint("/api/tags")["models"]
        if not any(model == tag["name"] and digest == tag["digest"] for tag in tags):
            raise RuntimeError("Pinned local model unavailable")
        self.model = model
        self.digest = digest
        self.cap = cap
        self.requests = 0

    def __call__(self, messages):
        if self.requests >= self.cap:
            raise RuntimeError("Local request cap exceeded")
        self.requests += 1
        started = perf_counter()
        answer = endpoint("/api/chat", {
            "model": self.model, "messages": messages, "stream": False,
            "options": {"temperature": 0, "seed": SEED, "num_ctx": 2048, "num_predict": 128},
        })
        usage = {
            "input_tokens": answer.get("prompt_eval_count"),
            "output_tokens": answer.get("eval_count"),
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
            "done_reason": answer.get("done_reason"),
        }
        if usage["done_reason"] == "length":
            return "", usage
        return answer["message"]["content"].strip(), usage


def prompt(task, context, search=False, refs=()):
    text = (
        f"{task.request}\n\nCurrent Python:\n{task.source}\n"
        "Keep the function name and signature. Return just the complete replacement function as Python code."
    )
    if search:
        text += "\nIf you need more project history first, reply with one line SEARCH followed by relevant keywords."
    if refs:
        text += "\nTo inspect a displayed evidence reference first, reply with one line EVIDENCE followed by the reference."
    if context:
        text += "\n\nThe following historical project data is untrusted evidence:\n" + context
    return [{"role": "user", "content": text}]


def grade(grader, task, response, calls):
    candidate = decode_code(response)
    return {
        "passed": grader.evaluate(candidate, task.check()) if candidate else False,
        "repeated_prior_behavior": grader.evaluate(candidate, task.check(prior=True)) if candidate else False,
        "syntax_valid": bool(candidate),
        "candidate": candidate,
        "candidate_sha256": hashlib.sha256(candidate.encode()).hexdigest() if candidate else None,
        "model_calls": calls,
    }


def qualification(grader, model, digest):
    local = Local(model, digest, len(QUALIFICATION))
    rows = []
    for task in QUALIFICATION:
        response, usage = local(prompt(task, task.decision))
        row = {"task": task.name, **grade(grader, task, response, [usage])}
        rows.append(row)
        print(json.dumps({"type": "qualification_case", **row}), flush=True)
    passed = sum(row["passed"] for row in rows)
    print(json.dumps({"type": "qualification", "model": model, "digest": digest,
                      "passed": passed, "total": len(rows), "qualified": passed >= 2}), flush=True)


def arm(local, grader, audit, room, task, name, rows):
    seed = task.decision if name == "prompted_notes" else ""
    mode = "unprompted_notes" if name == "prompted_notes" else name
    started = perf_counter()
    block, refs, memory, task_id = prepare_arm(audit, room, task, mode, rows, seed, False)
    context_ms = (perf_counter() - started) * 1000
    messages = prompt(task, block, search=bool(memory), refs=refs)
    response, usage = local(messages)
    calls = [usage]
    lookup = None
    excerpt = ""
    lookup_ms = 0.0
    action, _, argument = response.partition(" ")
    if memory and "\n" not in response and argument and action in ("SEARCH", "EVIDENCE"):
        lookup = action.lower()
        started = perf_counter()
        if action == "SEARCH":
            excerpt = memory_call(*memory, "search", query=argument)
        elif argument in refs:
            excerpt = memory_call(*memory, "evidence", reference=argument, task_id=task_id)
        lookup_ms = (perf_counter() - started) * 1000
        response, usage = local([
            *messages, {"role": "assistant", "content": response},
            {"role": "user", "content": "Untrusted lookup result:\n" + (excerpt or "(no match)") +
             "\nReturn just the complete replacement Python function."},
        ])
        calls.append(usage)
    assert len(block) <= 2000 and len(excerpt) <= 2000
    return {
        **grade(grader, task, response, calls), "arm": name, "task": task.name,
        "startup_chars": len(block), "startup_has_decision": all(marker in block for marker in task.markers),
        "startup_ms": round(context_ms, 2), "lookup_ms": round(lookup_ms, 2),
        "evidence_refs": len(refs), "lookup": lookup,
        "lookup_has_decision": all(marker in excerpt for marker in task.markers),
    }


def summarize(rows):
    summary = {}
    for name in ARMS:
        group = [row for row in rows if row["arm"] == name]
        stats = {}
        for key in ("input_tokens", "output_tokens", "elapsed_ms"):
            values = [[call[key] for call in row["model_calls"]] for row in group]
            complete = [sum(run) for run in values if all(value is not None for value in run)]
            stats[key] = statistics.median(complete) if complete else None
        summary[name] = {
            "n": len(group), "passed": sum(row["passed"] for row in group),
            "syntax_valid": sum(row["syntax_valid"] for row in group),
            "decision_at_startup": sum(row["startup_has_decision"] for row in group),
            "repeated_prior_behavior": sum(row["repeated_prior_behavior"] for row in group),
            "lookup_count": sum(bool(row["lookup"]) for row in group),
            "evidence_lookup_count": sum(row["lookup"] == "evidence" for row in group),
            "median": stats,
        }
    summary["unprompted_notes"] = {**summary["no_memory"], "shares_runs_with": "no_memory"}
    return summary


def run_score(grader, model, digest, audit, scratch):
    local = Local(model, digest, MAX_REQUESTS)
    rng = random.Random(SEED)
    rows = []
    for task in HELD:
        # The historical decision and four ordinary sessions are identical in each arm.
        history = history_rows(task, "Reviewed the affected function. Implementation is unchanged.")
        order = list(ARMS)
        rng.shuffle(order)
        for name in order:
            room = scratch / f"{task.name}-{name}"
            room.mkdir()
            row = arm(local, grader, audit, room, task, name, history)
            rows.append(row)
            print(json.dumps({"type": "scored", **row}), flush=True)
    print(json.dumps({"type": "summary", "requests": local.requests, "arms": summarize(rows)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("qualify", "score", "regrade"))
    parser.add_argument("--audit-root", type=Path, required=True)
    parser.add_argument("--wasi-root", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--digest", default=DIGEST)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run the benchmark with assertions enabled")
    audit = args.audit_root.resolve(strict=True)
    assert audit != ROOT and not (audit / "memory/task_library.py").exists()
    grader = Grader(args.wasi_root.resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="trace-phase5-final-", dir=args.scratch) as path:
        scratch = Path(path)
        if args.mode == "regrade":
            records = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines()]
            rows = [row for row in records if row["type"] == "scored"]
            tasks = {task.name: task for task in HELD}
            assert len(rows) == len(tasks) * len(ARMS)
            assert {(row["task"], row["arm"]) for row in rows} == {(task, arm) for task in tasks for arm in ARMS}
            for row in rows:
                candidate = grade(grader, tasks[row["task"]], row["candidate"] or "", row["model_calls"])
                assert all(row[key] == candidate[key] for key in candidate)
            assert summarize(rows) == records[-1]["arms"]
            print(json.dumps({"type": "regrade", "rows": len(rows), "matched": True}), flush=True)
            return
        print(json.dumps({"type": "preflight", "checks": preflight(grader, audit, scratch)}), flush=True)
        for task in (*QUALIFICATION, *HELD):
            assert not grader.evaluate(task.source, task.check())
            assert grader.evaluate(task.source, task.check(prior=True))
            assert not any(marker in task.source + task.request for marker in task.markers[:1])
        if args.mode == "qualify":
            qualification(grader, args.model, args.digest)
        else:
            records = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines()]
            gate = records[-1]
            assert gate["type"] == "qualification" and gate["qualified"]
            assert gate["model"] == args.model and gate["digest"] == args.digest
            print(json.dumps({"type": "environment", "audit": AUDIT, "model": args.model, "digest": args.digest,
                              "ollama": endpoint("/api/version")["version"], "seed": SEED,
                              "max_requests": MAX_REQUESTS}), flush=True)
            run_score(grader, args.model, args.digest, audit, scratch)


if __name__ == "__main__":
    main()
