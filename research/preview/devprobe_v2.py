"""Development diagnostic (NOT held-out evidence).

Replays the 24 already-scored maintained-notes tasks with the v2 editor
(mechanical extraction fallback + one equal-budget retry) and the
evidence-linked excerpt arm. The v2 qualification gate failed 2/3 for
qwen2.5-coder:7b, so the frozen scorer refuses held-out scoring; this probe
answers editor-mechanics questions on known tasks only and marks every row
`dev_probe: true`.
"""
import json, sys, time
from pathlib import Path

sys.path.insert(0, r"C:\Users\pruth\Documents\Codex\2026-11-14\referenced-chatgpt-conversation-this-is-an\work\Trace")
from research.preview import notes_trial as t
from research.preview.notes_cases import CASES, REPOS
from research.preview.trace2_evaluation import QWEN
from research.phase5.grader import Grader
from research.preview.effectiveness import emit

WORK = Path(r"C:\Users\pruth\Documents\Codex\2026-11-14\referenced-chatgpt-conversation-this-is-an\work")
OUT = WORK / "notes-audit" / "devprobe-v2-qwen-r0.jsonl"
WASI = Path(r"C:/Users/pruth/Documents/Codex/2026-11-11/referenced-chatgpt-conversation-this-is-an/work/p5-cpython")
REPOS_DIRS = {"trace": WORK / "Trace", "click": WORK / "eval-click", "packaging": WORK / "eval-packaging"}
SEED = t.SEEDS[0]
ARMS_ALL = (*t.ARMS, t.EXCERPT_ARM)


def record(payload):
    with OUT.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload) + "\n")
        f.flush()


def main():
    if OUT.exists():
        print("probe output exists; refusing to overwrite:", OUT)
        return
    grader = Grader(WASI, fuel=4_000_000_000)
    repos = REPOS_DIRS
    originals = {}
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as tmp:
        blocks, excerpts = t.contexts(Path(tmp), repos, excerpts=True)
    for case in CASES:
        original = t.definition(case, t.source(case, repos))
        originals[t.key(case)] = original
        reference = original.splitlines()[0] + "\n" + t.indent(case.reference, "    ")
        assert t.grade(grader, case, original) == (False, True), t.key(case)
        assert t.grade(grader, case, reference) == (True, True), t.key(case)
    record({"type": "environment", "dev_probe": True, "purpose": "editor-mechanics diagnostic on previously scored tasks",
            "model": QWEN, "editor": "v2", "repair": True, "arms": (*t.ARMS, t.EXCERPT_ARM), "seed": SEED,
            "trace_revision": "8de7c5bb045a9cf674ab92ac35ea1793d2450d52", "qualification": "2/3 (gate failed, excluded from held-out)"})
    started_total = time.perf_counter()
    for case in CASES:
        order = t.random_order(SEED, t.key(case), ARMS_ALL)
        for arm in order:
            context = {"trace": blocks[t.key(case)], t.EXCERPT_ARM: excerpts[t.key(case)],
                       "notes": case.note, "no_memory": ""}[arm]
            row = t.proposal(grader, case, originals[t.key(case)], QWEN, SEED, arm, context,
                             fenced_editor=True, repair=True)
            emit("scored", **row)
            record({"type": "scored", "dev_probe": True, **row})
    record({"type": "summary", "dev_probe": True,
            "minutes": round((time.perf_counter() - started_total) / 60, 1)})
    print("probe complete")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", write_through=True)
    main()
