"""Notes parity of TRACE's notes-first session start on the 24 frozen maintained-notes tasks.

DEVELOPMENT CHECK ON PREVIOUSLY SEEN TASKS. Rows carry ``dev_probe: true``.

Same frozen one-shot protocol as ``graphify_trial`` (fenced editor, editor v2,
one mechanical retry, temperature 0, ``num_ctx`` 2048, WASI grader). Arms:

  notes        the maintained note (``case.note``)
  trace        the previous automatic recall block (frozen seeding recipe, production renderer)
  trace_notes  what TRACE's native prompt hook injects now (``memory.dialogue.recall``), after the
               21 approved rules are committed to ``.trace/RULES.md`` on ``main`` in each memory room

This checks parity by construction and the plumbing behind it. It cannot show
recovery of notes failures: the notes here already hold the full rule.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from memory import dialogue
from research.preview import graphify_trial as gt
from research.preview import notes_trial as nt
from research.preview.notes_cases import CASES, REPOS

ARMS = ("notes", "trace", "trace_notes")


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=trial", "-c", "user.email=trial@example.invalid", "-c", "commit.gpgsign=false",
                    *args], cwd=root, check=True, capture_output=True)


def build_contexts(args) -> dict:
    repos = {name: getattr(args, name) for name in REPOS}
    contexts = {}
    with TemporaryDirectory(prefix="notes-first-", dir=args.scratch) as directory:
        root = Path(directory) / "rooms"
        root.mkdir()
        blocks = gt.trace_blocks(root, repos)  # previous output, built before the rooms become repositories
        for repo in REPOS:
            room = root / repo
            rules = [f"## {case.path} :: {case.symbol}\n- {case.note}\n" for case in CASES if case.repo == repo and case.note]
            (room / ".trace").mkdir()
            (room / ".trace" / "RULES.md").write_text("\n".join(rules), encoding="utf-8")
            git(room, "init", "-q", "-b", "main")
            git(room, "add", ".trace/RULES.md")
            git(room, "commit", "-q", "-m", "approved rules")
            for case in (case for case in CASES if case.repo == repo):
                injected = dialogue.recall(room / ".agent-memory" / "memory.db", "new-session",
                                           f"{case.request} File: {case.path}")
                contexts[nt.key(case)] = {"notes": case.note, "trace": blocks[nt.key(case)], "trace_notes": injected}
    return contexts


def retrieval_report(contexts: dict, frozen: dict | None) -> list[dict]:
    return [{"task": case.symbol, "history": case.history,
             "trace_notes_equals_note": contexts[nt.key(case)]["trace_notes"] == case.note,
             "trace_equals_frozen": None if frozen is None else contexts[nt.key(case)]["trace"] == frozen[nt.key(case)]["trace"],
             **{f"{arm}_chars": len(contexts[nt.key(case)][arm]) for arm in ARMS}} for case in CASES]


def summarize(args):
    from statistics import median
    rows, duplicates = gt.deduplicated([row for row in nt.read_rows(args.output) if row["type"] == "scored"])
    print(f"distinct runs {len(rows)}; identical duplicate rows collapsed {duplicates}")
    by = {(r["task"], r["arm"]): r for r in rows}
    tasks = sorted({r["task"] for r in rows}, key=lambda t: t.split(":")[-1])
    print(f"{'arm':12} {'pass':>6} {'retained':>9} {'lost':>5} {'recovered':>10} {'unresolved':>11} {'regr':>5} {'med_in':>7}")
    for arm in ARMS:
        sample = [by[t, arm] for t in tasks if (t, arm) in by]
        pairs = [(by[t, "notes"]["passed"], by[t, arm]["passed"]) for t in tasks if (t, arm) in by and (t, "notes") in by]
        print(f"{arm:12} {sum(r['passed'] for r in sample):>3}/{len(sample):<2} {pairs.count((True, True)):>9} "
              f"{pairs.count((True, False)):>5} {pairs.count((False, True)):>10} {pairs.count((False, False)):>11} "
              f"{sum(r['regression'] and r['candidate'] is not None for r in sample):>5} "
              f"{median(r['input_tokens'] or 0 for r in sample):>7.0f}")
    same = [t for t in tasks if (t, "trace_notes") in by and by[t, "trace_notes"]["context_hash"] == by[t, "notes"]["context_hash"]]
    differ = [t.split(":")[-1] for t in same if by[t, "trace_notes"]["candidate"] != by[t, "notes"]["candidate"]]
    print(f"trace_notes context identical to the note on {len(same)}/{len(tasks)} tasks; "
          f"different candidate despite identical prompt: {len(differ)} {differ}")
    for arm in ("trace", "trace_notes"):
        lost = [t.split(":")[-1] for t in tasks if by[t, "notes"]["passed"] and not by[t, arm]["passed"]]
        gained = [t.split(":")[-1] for t in tasks if by[t, arm]["passed"] and not by[t, "notes"]["passed"]]
        print(f"  {arm} vs notes: lost {lost}  recovered {gained}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", write_through=True)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("contexts", "run", "regrade", "summary"))
    for name in REPOS:
        parser.add_argument(f"--{name}", type=Path)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--wasi", type=Path)
    parser.add_argument("--contexts", type=Path, required=True)
    parser.add_argument("--frozen", type=Path, help="graphify-results/contexts.json, to confirm the trace arm is unchanged")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--regrade-output", type=Path)
    parser.add_argument("--run", default="greedy")
    parser.add_argument("--seed", type=int, action="append")
    args = parser.parse_args()
    args.arm, args.experiment = list(ARMS), "notes_first_trial"
    args.seed = args.seed or [nt.SEEDS[0]]
    if args.mode == "contexts":
        contexts = build_contexts(args)
        args.contexts.write_text(json.dumps(contexts, indent=1, ensure_ascii=False), encoding="utf-8")
        frozen = json.loads(args.frozen.read_text(encoding="utf-8")) if args.frozen else None
        for row in retrieval_report(contexts, frozen):
            print(json.dumps(row))
    elif args.mode == "run":
        gt.run(args)
    elif args.mode == "regrade":
        gt.regrade(args)
    else:
        summarize(args)


if __name__ == "__main__":
    main()
