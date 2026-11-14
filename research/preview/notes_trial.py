"""Compare TRACE with maintained notes on fresh pinned repository functions."""

from __future__ import annotations

import argparse
import ast
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
from random import Random
from secrets import token_hex
from statistics import median
import subprocess
import sys
from tempfile import TemporaryDirectory
from textwrap import indent
from time import perf_counter

from memory import episodic, session_context, store, synthetic, task_library, transcript
from research.phase5.grader import Grader
from research.preview.definition_trial import function_only
from research.preview.effectiveness import emit, request
from research.preview.notes_cases import CASES, QUALIFICATION, REPOS
from research.preview.repository_js import show
from research.preview.trace2_evaluation import DIGEST, QWEN
from research.preview.matched_definitions import GEMMA, GEMMA_DIGEST
from trace_memory import project

MODELS = {QWEN: DIGEST, GEMMA: GEMMA_DIGEST}
ARMS = ("no_memory", "notes", "trace")
SEEDS = (52183, 52189, 52193)
MAX_OUTPUT = 320
WRONG = "TRACE_WRONG_TASK_MARKER"
RETIRED = "TRACE_RETIRED_MARKER"
CHECK_HELPER = """
def raises(error, func, *args):
    try:
        func(*args)
    except error:
        return True
    return False

def check(probe):
    try:
        return bool(probe())
    except Exception:
        return False
"""


def key(case):
    return f"{case.repo}:{case.path}:{case.symbol}"


def protocol_digest():
    files = (Path(__file__), Path(__file__).with_name("notes_cases.py"))
    return sha256(b"".join(path.read_bytes().replace(b"\r\n", b"\n") for path in files)).hexdigest()


def source(case, repos):
    return show(repos[case.repo], REPOS[case.repo], case.path)


def definition(case, text):
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef) and node.name == case.symbol:
            if ast.get_docstring(node):
                node.body.pop(0)
            return ast.unparse(node)
    raise ValueError(f"missing pinned definition: {key(case)}")


def grade(grader, case, candidate):
    if candidate is None:
        return False, False
    marker = token_hex(12)
    script = (f"from __future__ import annotations\n{case.prelude}\n{CHECK_HELPER}\n{candidate}\n"
              f"print({marker!r}, check(lambda: {case.changed}), check(lambda: {case.preserved}))")
    ok, output = grader.run(script)
    verdict = output.decode("ascii", errors="replace").strip().split()
    if not ok or len(verdict) != 3 or verdict[0] != marker:
        return False, False
    return verdict[1] == "True", verdict[2] == "True"


def memory_room(root, repo, cases, repos):
    root.mkdir()
    for name in {case.path for case in cases}:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(show(repos[repo], REPOS[repo], name).encode("utf-8"))
    project.init_project(root)
    db = project.db_path(root)
    with closing(store.connect(db)) as conn:
        other = task_library.create_task(conn, "Separate investigation")["id"]
        for index, case in enumerate(cases):
            if not case.history:
                continue
            history = f"history-{index}"
            retired = transcript.record_message(
                conn, session_id=history, role="user",
                content=f"{case.symbol} review: previous value {RETIRED}.",
            )
            prior = episodic.record_event(
                conn, repo=repo, type="decision", file=case.path, symbol=case.symbol,
                payload={"decision": f"{case.symbol} planned {RETIRED}"},
                evidence=[{"type": "transcript", "message_id": retired}],
            )
            evidence = transcript.record_message(conn, session_id=history, role="user", content=case.note)
            episodic.record_event(
                conn, repo=repo, type="decision", file=case.path, symbol=case.symbol,
                payload={"decision": case.note, "status": "verified", "supersedes": prior},
                evidence=[{"type": "transcript", "message_id": evidence}],
            )
            episodic.record_event(
                conn, repo=repo, type="decision", file=case.path, symbol=case.symbol, task_id=other,
                payload={"decision": f"{case.symbol} approved {WRONG}", "status": "verified"},
            )
        with conn:
            for index, case in enumerate(cases):
                for n in range(36):
                    episodic.record_event(
                        conn, autocommit=False, repo=repo, type="observation", file=case.path, symbol=case.symbol,
                        payload={"message": f"Routine review {case.symbol} {n}: reran local diagnostics."},
                    )
                    transcript.record_message(
                        conn, autocommit=False, session_id=f"routine-{index}", role="assistant",
                        content=f"Reviewed {case.symbol} and confirmed diagnostics pending, update {n}.",
                    )
    return db


def contexts(root, repos):
    outputs = {}
    for repo in REPOS:
        cases = [case for case in CASES if case.repo == repo]
        room = root / repo
        db = memory_room(room, repo, cases, repos)
        for case in cases:
            started = perf_counter()
            query = f"{case.request} File: {case.path}"
            result = session_context.get_session_start_context(
                db, exclude_session_id="new-session", query=query, max_items=4, max_chars=3000, project_root=room,
            )
            block = synthetic.render_user_block(result["items"])
            assert len(block) <= 3000 and WRONG not in block
            verified = any(item.get("text") == case.note and item.get("evidence")
                           for item in result["items"] if item["kind"] == "finding")
            outputs[key(case)] = block
            emit("recall", task=key(case), history=case.history, fact=bool(case.note and case.note in block),
                 evidence=verified, retired=RETIRED in block, chars=len(block),
                 elapsed_ms=round((perf_counter() - started) * 1000, 2))
    return outputs


def proposal(grader, case, original, model, seed, arm, context):
    prompt = (f"{case.request}\nFile: {case.path}\n"
              "Return only the complete replacement Python function with the same signature. "
              "Use the approved rule in the historical project data when available. Preserve unrelated behavior.\n\n")
    prompt += (
        f"\nCurrent function and supporting definitions:\n{case.prelude}\n{original}\n\n"
        f"Historical project data:\n{context or '(none)'}\n\n"
        "Implement any approved rule for this function. Return a JSON object with a short rule in "
        "the plan field and the complete replacement source in the function field."
    )
    started = perf_counter()
    try:
        reply = request("/api/chat", {
            "model": model, "stream": False, "keep_alive": "10m",
            "format": {"type": "object", "properties": {"plan": {"type": "string"}, "function": {"type": "string"}},
                       "required": ["plan", "function"], "additionalProperties": False},
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0, "seed": seed, "num_ctx": 2048, "num_predict": MAX_OUTPUT, "num_gpu": 0},
        })
        reason = reply.get("done_reason")
        try:
            body = json.loads(reply["message"]["content"])
            updated = body.get("function") if isinstance(body, dict) else None
        except (TypeError, ValueError):
            updated = None
        candidate = function_only(updated, case.symbol, original) if reason != "length" and isinstance(updated, str) else None
    except (OSError, RuntimeError) as exc:
        reply, candidate, reason = {}, None, type(exc).__name__
    changed, preserved = grade(grader, case, candidate)
    return dict(task=key(case), arm=arm, model=model, seed=seed, candidate=candidate, passed=changed and preserved,
                regression=not preserved, reason=reason, input_tokens=reply.get("prompt_eval_count"),
                output_tokens=reply.get("eval_count"), elapsed_ms=round((perf_counter() - started) * 1000, 2),
                context_chars=len(context), context_hash=sha256(context.encode()).hexdigest())


def summarize(rows):
    scored = [row for row in rows if row["type"] == "scored"]
    grouped = {(r["task"], r["model"], r["seed"], r["arm"]): r for r in scored}
    assert len(grouped) == len(scored)
    stats = {}
    for arm in ARMS:
        sample = [row for row in scored if row["arm"] == arm]
        if not sample:
            continue
        stats[arm] = dict(passed=sum(r["passed"] for r in sample), total=len(sample),
                          regressions=sum(r["regression"] for r in sample),
                          median_ms=median(r["elapsed_ms"] for r in sample),
                          median_input_tokens=median([r["input_tokens"] for r in sample if r["input_tokens"]] or [0]))
    differences = {}
    for task, model, seed, arm in grouped:
        if arm == "trace" and (task, model, seed, "notes") in grouped:
            row = grouped[task, model, seed, "trace"]
            comparison = grouped[task, model, seed, "notes"]
            differences.setdefault(task, []).append(int(row["passed"]) - int(comparison["passed"]))
    means = {task: sum(values) / len(values) for task, values in differences.items()}
    cohorts = [[value for task, value in means.items() if task.startswith(repo + ":")] for repo in REPOS]
    rng = Random(SEEDS[0])
    draws = sorted(
        sum(sum(rng.choices(cohort, k=len(cohort))) for cohort in cohorts) / len(means)
        for _ in range(10000)
    ) if means else []
    expected = {(key(case), model, seed, arm) for case in CASES
                for model in MODELS for seed in SEEDS for arm in ARMS}
    return dict(arms=stats, paired_tasks=len(means), paired_runs=sum(map(len, differences.values())),
                complete_matrix=set(grouped) == expected,
                lift=sum(means.values()) / len(means) if means else None,
                paired_bootstrap_95=[draws[250], draws[9749]] if draws else None)


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()]


def run(args, root):
    repos = {name: getattr(args, name) for name in REPOS}
    if (len(CASES) != 24 or len({key(case) for case in CASES}) != 24
            or len({key(case) for case in (*CASES, *QUALIFICATION)}) != 27):
        raise ValueError("the frozen task inventory has changed")
    if args.mode == "recall":
        contexts(root, repos)
        return
    grader = Grader(args.wasi, fuel=4_000_000_000)
    canary = root / "host-canary.txt"
    canary.write_text("outside grader", encoding="utf-8")
    grader.verify_boundary(canary)
    originals = {}
    for case in (*QUALIFICATION, *CASES):
        original = definition(case, source(case, repos))
        originals[key(case)] = original
        reference = original.splitlines()[0] + "\n" + indent(case.reference, "    ")
        assert grade(grader, case, original) == (False, True), key(case)
        assert grade(grader, case, reference) == (True, True), key(case)
    emit("preflight", held_out=len(CASES), qualification=len(QUALIFICATION), repositories=REPOS)
    if args.mode == "preflight":
        return
    if args.mode == "regrade":
        rows = read_rows(args.input)
        cases = {key(case): case for case in (*QUALIFICATION, *CASES)}
        selected = [row for row in rows if row["type"] in ("qualification", "scored")]
        for row in selected:
            case = cases[row["task"]]
            candidate = row["candidate"]
            if candidate is not None:
                assert function_only(candidate, case.symbol, originals[row["task"]]) == candidate
            changed, preserved = grade(grader, case, candidate)
            assert (changed and preserved, not preserved) == (row["passed"], row["regression"]), row["task"]
        emit("regrade", checked=len(selected), **summarize(rows))
        return
    tags = {row["name"]: row["digest"] for row in request("/api/tags")["models"]}
    emit("environment", model=args.model, model_digest=MODELS[args.model], num_gpu=0, context_tokens=2048,
         max_output_tokens=MAX_OUTPUT, seeds=SEEDS, ollama=request("/api/version")["version"],
         protocol_sha256=protocol_digest(),
         trace_revision=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    assert tags.get(args.model) == MODELS[args.model]
    if args.mode == "qualify":
        rows = []
        for case in QUALIFICATION:
            row = proposal(grader, case, originals[key(case)], args.model, SEEDS[0], "qualification", case.note)
            rows.append(row)
            emit("qualification", **row)
        emit("qualification_summary", model=args.model, passed=sum(r["passed"] for r in rows), required=len(rows))
        return
    qualified = read_rows(args.input)
    assert any(r["type"] == "environment" and r["model_digest"] == tags[args.model]
               and r.get("protocol_sha256") == protocol_digest() for r in qualified)
    qualification = [r for r in qualified if r["type"] == "qualification" and r["model"] == args.model]
    assert len(qualification) == len(QUALIFICATION) and all(
        any(row["task"] == key(case) and row["passed"]
            and grade(grader, case, row["candidate"]) == (True, True) for row in qualification)
        for case in QUALIFICATION
    )
    memory = root / "memory"
    memory.mkdir()
    blocks = contexts(memory, repos)
    # Shards preserve identical per-task arm order and allow independent repeats.
    cases = CASES if args.repo is None else [case for case in CASES if case.repo == args.repo]
    rows = []
    for repeat in args.repeat or range(3):
        seed = SEEDS[repeat]
        for case in cases:
            order = random_order(seed, key(case))
            for arm in order:
                context = blocks[key(case)] if arm == "trace" else case.note if arm == "notes" else ""
                row = proposal(grader, case, originals[key(case)], args.model, seed, arm, context)
                rows.append({"type": "scored", **row})
                emit("scored", **row)
    emit("summary", **summarize(rows))


def random_order(seed, task):
    return Random(f"{seed}:{task}").sample(ARMS, len(ARMS))


def main():
    sys.stdout.reconfigure(encoding="utf-8", write_through=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "recall", "qualify", "score", "regrade"))
    for name in REPOS:
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--wasi", type=Path)
    parser.add_argument("--model", choices=MODELS)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--repo", choices=REPOS)
    parser.add_argument("--repeat", type=int, choices=range(3), action="append")
    args = parser.parse_args()
    if (not __debug__ or (args.mode != "recall" and args.wasi is None)
            or (args.mode in ("qualify", "score") and args.model is None)
            or (args.mode in ("score", "regrade") and args.input is None)
            or (args.repeat and len(set(args.repeat)) != len(args.repeat))):
        parser.error("use assertions, provide WASI/model/input when required, and select unique repeats")
    with TemporaryDirectory(prefix="trace-notes-", dir=args.scratch) as directory:
        run(args, Path(directory))


if __name__ == "__main__":
    main()
