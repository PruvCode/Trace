"""Graphify versus TRACE versus maintained notes on the 24 frozen maintained-notes tasks.

DEVELOPMENT COMPARISON ON PREVIOUSLY SEEN TASKS. Rows carry ``dev_probe: true``.

Every arm uses the frozen one-shot protocol unchanged (``notes_trial.proposal``
with the fenced editor, editor v2 extraction and its one mechanical retry,
temperature 0, ``num_ctx`` 2048, 400 output tokens, WASI grader). Only the
"Project note" context differs:

  no_memory         empty
  notes             the maintained note (``case.note``)
  trace             TRACE's automatic recall from the frozen seeding recipe (production renderer)
  graphify_code     ``graphify query "<request> File: <path>" --budget 750`` over a code-only
                    Graphify graph of the pinned repository (local tree-sitter, no LLM)
  graphify_history  the same query over a Graphify graph of the pinned repository plus the
                    authored decision history as markdown (superseded, approved and other-task
                    decisions, and the user messages stating them), extracted by Graphify's
                    local Ollama backend with the same Qwen model. The 36 routine observations
                    per task that TRACE also receives were left out (CPU extraction cost), which
                    makes Graphify's retrieval task easier than TRACE's.

The 750-token budget matches TRACE's 3,000-character recall cap.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from memory import session_context, synthetic
from research.phase5.grader import Grader
from research.preview import notes_trial as nt
from research.preview.effectiveness import request as ollama
from research.preview.notes_cases import CASES, REPOS

ARMS = ("no_memory", "notes", "trace", "graphify_code", "graphify_history")
GRAPHIFY = Path.home() / "graphify-eval"
BUDGET = 750


def sha(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def graphify_context(graph: Path, case) -> str:
    env = {**os.environ, "GRAPHIFY_QUERY_LOG_DISABLE": "1", "GRAPHIFY_BACKEND": "none"}
    done = subprocess.run(
        [str(GRAPHIFY / ".venv/Scripts/graphify.exe"), "query", f"{case.request} File: {case.path}",
         "--budget", str(BUDGET), "--graph", str(graph)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300,
    )
    if done.returncode not in (0, 3, 4):
        raise RuntimeError(f"graphify query failed for {case.symbol}: {done.stderr[-500:]}")
    return done.stdout.strip()


def trace_blocks(root: Path, repos: dict) -> dict:
    blocks = {}
    for repo in REPOS:
        cases = [case for case in CASES if case.repo == repo]
        room = root / repo
        db = nt.memory_room(room, repo, cases, repos)
        for case in cases:
            result = session_context.get_session_start_context(
                db, exclude_session_id="new-session", query=f"{case.request} File: {case.path}",
                max_items=4, max_chars=3000, project_root=room)
            block = synthetic.render_user_block(result["items"])
            assert len(block) <= 3000 and nt.WRONG not in block
            blocks[nt.key(case)] = block
    return blocks


def build_contexts(args) -> dict:
    repos = {name: getattr(args, name) for name in REPOS}
    with TemporaryDirectory(prefix="graphify-trial-", dir=args.scratch) as directory:
        root = Path(directory) / "rooms"
        root.mkdir()
        trace = trace_blocks(root, repos)
    contexts = {}
    for case in CASES:
        key = nt.key(case)
        code = graphify_context(GRAPHIFY / "code" / case.repo / "graphify-out" / "graph.json", case)
        history = graphify_context(GRAPHIFY / "history" / case.repo / "graphify-out" / "graph.json", case)
        contexts[key] = {"no_memory": "", "notes": case.note, "trace": trace[key],
                         "graphify_code": code, "graphify_history": history}
    return contexts


def retrieval_report(contexts: dict) -> list[dict]:
    rows = []
    for case in CASES:
        c = contexts[nt.key(case)]
        rows.append({"task": case.symbol, "history": case.history, **{
            f"{arm}_has_rule": bool(case.note) and case.note in c[arm] for arm in ("trace", "graphify_code", "graphify_history")},
            "graphify_history_mentions_symbol_decision": f"{case.symbol} in {case.path}" in c["graphify_history"],
            "graphify_history_stale_marker": nt.RETIRED in c["graphify_history"],
            "graphify_history_wrong_task_marker": nt.WRONG in c["graphify_history"],
            **{f"{arm}_chars": len(c[arm]) for arm in ARMS}})
    return rows


def run_key(row) -> tuple:
    return (row["task"], row["arm"], row["seed"], row["run"], row["context_hash"])


def deduplicated(rows):
    kept, duplicates = {}, 0
    for row in rows:
        key = run_key(row)
        if key in kept:
            if (kept[key]["candidate"], kept[key]["passed"]) != (row["candidate"], row["passed"]):
                raise ValueError(f"conflicting duplicate rows for {key}")
            duplicates += 1
            continue
        kept[key] = row
    return list(kept.values()), duplicates


def setup_grader(args):
    repos = {name: getattr(args, name) for name in REPOS}
    grader = Grader(args.wasi, fuel=4_000_000_000)
    canary = args.scratch / "host-canary.txt"
    canary.write_text("outside grader", encoding="utf-8")
    grader.verify_boundary(canary)
    originals = {}
    for case in CASES:
        original = nt.definition(case, nt.source(case, repos))
        reference = original.splitlines()[0] + "\n" + nt.indent(case.reference, "    ")
        assert nt.grade(grader, case, original) == (False, True), nt.key(case)
        assert nt.grade(grader, case, reference) == (True, True), nt.key(case)
        originals[nt.key(case)] = original
    return grader, originals


def run(args):
    contexts = json.loads(args.contexts.read_text(encoding="utf-8"))
    grader, originals = setup_grader(args)
    tags = {row["name"]: row["digest"] for row in ollama("/api/tags")["models"]}
    assert tags.get(nt.QWEN) == nt.MODELS[nt.QWEN]
    done = set()
    if args.output.exists():
        done = {run_key(row) for row in nt.read_rows(args.output) if row["type"] == "scored"}
    nt.record_row(args.output, {
        "type": "environment", "dev_probe": True, "experiment": getattr(args, "experiment", "graphify_trial"), "run": args.run,
        "model": nt.QWEN, "model_digest": tags[nt.QWEN], "ollama": ollama("/api/version")["version"],
        "graphify": subprocess.run([str(GRAPHIFY / ".venv/Scripts/python.exe"), "-c",
                                    "import importlib.metadata as m; print(m.version('graphifyy'))"],
                                   capture_output=True, text=True).stdout.strip(),
        "trace_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "protocol_sha256": nt.protocol_digest(), "contexts_sha256": sha(args.contexts.read_text(encoding="utf-8")),
        "arms": args.arm, "seeds": args.seed, "fenced_editor": True, "editor_v2": True, "num_ctx": 2048,
    })
    for seed in args.seed:
        for case in CASES:
            key = nt.key(case)
            for arm in nt.random_order(seed, key, tuple(args.arm)):
                context = contexts[key][arm]
                if (key, arm, seed, args.run, sha(context)) in done:
                    continue
                row = nt.proposal(grader, case, originals[key], nt.QWEN, seed, arm, context,
                                  fenced_editor=True, repair=True)
                if row["reason"] in ("OSError", "RuntimeError", "TimeoutError", "ConnectionRefusedError",
                                     "ConnectionResetError", "RemoteDisconnected"):
                    nt.record_row(args.output, {"type": "infra_error", "task": key, "arm": arm, "seed": seed,
                                                "reason": row["reason"]})
                    continue
                nt.record_row(args.output, {"type": "scored", "dev_probe": True, "run": args.run, **row})
                print(json.dumps({"task": case.symbol, "arm": arm, "passed": row["passed"],
                                  "regression": row["regression"], "valid": row["candidate"] is not None,
                                  "in_tok": row["input_tokens"], "s": round(row["elapsed_ms"] / 1000)}), flush=True)


def regrade(args):
    grader, _ = setup_grader(args)
    cases = {nt.key(case): case for case in CASES}
    rows = [row for row in nt.read_rows(args.output) if row["type"] == "scored"]
    mismatches = [(r["task"], r["arm"]) for r in rows
                  if (lambda v: (v[0] and v[1], not v[1]))(nt.grade(grader, cases[r["task"]], r["candidate"]))
                  != (r["passed"], r["regression"])]
    report = {"type": "regrade", "dev_probe": True, "checked": len(rows), "reproduced": len(rows) - len(mismatches),
              "mismatches": mismatches}
    nt.record_row(args.regrade_output, report)
    print(json.dumps(report))
    assert not mismatches


def summarize(args):
    from statistics import median
    rows, duplicates = deduplicated([row for row in nt.read_rows(args.output) if row["type"] == "scored"])
    print(f"distinct runs {len(rows)}; identical duplicate rows collapsed {duplicates}")
    print(f"{'arm':18} {'pass':>6} {'hist':>6} {'ctrl':>6} {'regr':>5} {'valid':>6} {'med_in':>7} {'max_in':>7} {'med_ctx':>8}")
    history = {nt.key(c) for c in CASES if c.history}
    for arm in ARMS:
        s = [r for r in rows if r["arm"] == arm]
        if not s:
            continue
        print(f"{arm:18} {sum(r['passed'] for r in s):>3}/{len(s):<2} {sum(r['passed'] for r in s if r['task'] in history):>3}/21"
              f" {sum(r['passed'] for r in s if r['task'] not in history):>3}/3 {sum(r['regression'] and r['candidate'] is not None for r in s):>5}"
              f" {sum(r['candidate'] is not None for r in s):>6} {median(r['input_tokens'] or 0 for r in s):>7.0f}"
              f" {max(r['input_tokens'] or 0 for r in s):>7} {median(r['context_chars'] for r in s):>8.0f}")
    by = {(r["task"], r["arm"]): r for r in rows}
    tasks = sorted({r["task"] for r in rows}, key=lambda t: t.split(":")[-1])
    print("\n" + f"{'task':22}" + "".join(f"{a:>17}" for a in ARMS))
    for t in tasks:
        cells = []
        for a in ARMS:
            r = by.get((t, a))
            cells.append("-" if r is None else "P" if r["passed"] else ("R" if r["regression"] and r["candidate"] else "F"))
        print(f"{t.split(':')[-1]:22}" + "".join(f"{c:>17}" for c in cells))
    for left in ("graphify_code", "graphify_history", "trace"):
        for right in ("notes", "no_memory", "trace"):
            if left == right:
                continue
            gain = [t.split(":")[-1] for t in tasks if (t, left) in by and (t, right) in by and by[t, left]["passed"] and not by[t, right]["passed"]]
            loss = [t.split(":")[-1] for t in tasks if (t, left) in by and (t, right) in by and by[t, right]["passed"] and not by[t, left]["passed"]]
            print(f"  {left} vs {right}: +{len(gain)} {gain}  -{len(loss)} {loss}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", write_through=True)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("contexts", "run", "regrade", "summary"))
    for name in REPOS:
        parser.add_argument(f"--{name}", type=Path)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--wasi", type=Path)
    parser.add_argument("--contexts", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--regrade-output", type=Path)
    parser.add_argument("--run", default="greedy")
    parser.add_argument("--seed", type=int, action="append")
    parser.add_argument("--arm", action="append", choices=ARMS)
    args = parser.parse_args()
    if not __debug__:
        parser.error("assertions are part of the protocol; do not run with -O")
    args.seed = args.seed or [nt.SEEDS[0]]
    args.arm = args.arm or list(ARMS)
    if args.mode == "contexts":
        contexts = build_contexts(args)
        args.contexts.write_text(json.dumps(contexts, ensure_ascii=False, indent=1), encoding="utf-8")
        report = retrieval_report(contexts)
        args.contexts.with_name("retrieval.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        for row in report:
            print(json.dumps(row))
    elif args.mode == "run":
        run(args)
    elif args.mode == "regrade":
        regrade(args)
    else:
        summarize(args)


if __name__ == "__main__":
    main()
