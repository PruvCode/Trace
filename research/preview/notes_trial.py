"""Compare TRACE with maintained notes on fresh pinned repository functions."""

from __future__ import annotations

import argparse
import ast
from contextlib import closing
from hashlib import sha256
import json
import os
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
from research.phase5.grader import Grader, MAX_SOURCE
from research.preview import editor
from research.preview.definition_trial import function_only
from research.preview.effectiveness import emit, request
from research.preview.notes_cases import CASES, QUALIFICATION, QUALIFICATION_V2, REPOS
from research.preview.repository_js import show
from research.preview.trace2_evaluation import DIGEST, QWEN
from research.preview.matched_definitions import GEMMA, GEMMA_DIGEST
from trace_memory import project

MODELS = {QWEN: DIGEST, GEMMA: GEMMA_DIGEST}
ARMS = ("no_memory", "notes", "trace")
EXCERPT_ARM = "trace_min"
SEEDS = (52183, 52189, 52193)
MAX_OUTPUT = 320
FENCED_OUTPUT = 400
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
    files = (Path(__file__), Path(__file__).with_name("notes_cases.py"), Path(editor.__file__))
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


def function_candidate(reply: str, case, original: str) -> tuple[str | None, str | None]:
    """Strict v1 extraction first, then the v2 fallback, never the graded checks."""
    candidate = function_only(reply, case.symbol, original)
    if candidate is not None:
        return candidate, None
    return editor.extract_function(reply, case.symbol, original)


def retry_message(defect: str | None) -> str:
    return editor.retry_feedback(defect or "no-block", "")


def contexts(root, repos, *, excerpts=False):
    outputs = {}
    excerpts_out = {}
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
            if excerpts:
                excerpts_out[key(case)] = editor.render_excerpt(result["items"])
            emit("recall", task=key(case), history=case.history, fact=bool(case.note and case.note in block),
                 evidence=verified, retired=RETIRED in block, chars=len(block),
                 excerpt_chars=len(excerpts_out[key(case)]) if excerpts else None,
                 elapsed_ms=round((perf_counter() - started) * 1000, 2))
    return (outputs, excerpts_out) if excerpts else outputs


def proposal(grader, case, original, model, seed, arm, context, *, fenced_editor=False, repair=False):
    prompt = (f"{case.request}\nFile: {case.path}\n"
              "Return only the complete replacement Python function with the same signature. "
              "Use the approved rule in the historical project data when available. Preserve unrelated behavior.\n\n")
    prompt += (
        f"\nCurrent function and supporting definitions:\n{case.prelude}\n{original}\n\n"
        f"Historical project data:\n{context or '(none)'}\n\n"
        "Implement any approved rule for this function. Return a JSON object with a short rule in "
        "the plan field and the complete replacement source in the function field."
    )
    schema = {"type": "object", "properties": {"plan": {"type": "string"}, "function": {"type": "string"}},
              "required": ["plan", "function"], "additionalProperties": False}
    if fenced_editor:
        prompt = (
            "Implement the user request by making the smallest code change. The project note gives the accepted rule "
            "for this change. Retain every part of the existing function that the rule does not change. Respond with "
            "the complete updated Python function in a Python code block.\n\n"
            f"User request: {case.request}\n"
            f"Project note: {context or '(none)'}\n\n"
            + (f"Supporting definitions:\n```python\n{case.prelude}\n```\n\n" if case.prelude else "")
            + f"```python\n{original}\n```"
        )
    started = perf_counter()
    candidate, reason, defect = None, None, None
    attempts = 0
    input_tokens = output_tokens = 0
    for attempt in range(2 if repair else 1):
        message = prompt if attempt == 0 else (
            f"{prompt}\n\nPrevious reply:\n{reply}\n\n{retry_message(defect)}")
        try:
            reply = request("/api/chat", {
                "model": model, "stream": False, "keep_alive": "10m",
                **({} if fenced_editor else {"format": schema}),
                "messages": [{"role": "user", "content": message}],
                "options": {"temperature": 0, "seed": seed, "num_ctx": 2048,
                            "num_predict": FENCED_OUTPUT if fenced_editor else MAX_OUTPUT, "num_gpu": 0},
            })
        except (OSError, RuntimeError) as exc:
            reason, candidate, defect = type(exc).__name__, None, None
            break
        attempts += 1
        input_tokens += reply.get("prompt_eval_count") or 0
        output_tokens += reply.get("eval_count") or 0
        reason = reply.get("done_reason")
        updated = None
        if reason != "length":
            try:
                content = reply["message"]["content"]
                body = {"function": content} if fenced_editor else json.loads(content)
                updated = body.get("function") if isinstance(body, dict) else None
            except (TypeError, ValueError):
                updated = None
        if isinstance(updated, str) and len(updated) <= MAX_SOURCE:
            candidate, defect = function_candidate(updated, case, original)
        else:
            candidate, defect = None, None
        if candidate is not None or not repair or reason == "length":
            break
    changed, preserved = grade(grader, case, candidate)
    return dict(task=key(case), arm=arm, model=model, seed=seed, candidate=candidate, passed=changed and preserved,
                regression=not preserved, reason=reason, attempts=attempts, defect=defect if candidate is None else None,
                input_tokens=input_tokens or None,
                output_tokens=output_tokens or None, elapsed_ms=round((perf_counter() - started) * 1000, 2),
                context_chars=len(context), context_hash=sha256(context.encode()).hexdigest())


def summarize(rows, arms=None):
    scored = [row for row in rows if row["type"] == "scored"]
    dev_probe = any(row.get("dev_probe") for row in scored)
    if dev_probe and not all(row.get("dev_probe") for row in scored):
        raise ValueError("development results must stay separate from qualified scores")
    grouped = {(r["task"], r["model"], r["seed"], r["arm"]): r for r in scored}
    assert len(grouped) == len(scored)
    arms = ARMS if arms is None else arms
    stats = {}
    for arm in arms:
        sample = [row for row in scored if row["arm"] == arm]
        if not sample:
            continue
        stats[arm] = dict(passed=sum(r["passed"] for r in sample), total=len(sample),
                          regressions=sum(r["regression"] for r in sample if r["candidate"] is not None),
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
                for model in MODELS for seed in SEEDS for arm in arms}
    return dict(arms=stats, paired_tasks=len(means), paired_runs=sum(map(len, differences.values())),
                dev_probe=dev_probe, complete_matrix=not dev_probe and set(grouped) == expected,
                lift=sum(means.values()) / len(means) if means else None,
                paired_bootstrap_95=[draws[250], draws[9749]] if draws else None)


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()]


def record_row(path, payload):
    """Append one row durably so an interrupted run can be resumed and audited."""
    if path is None:
        return
    handle = open(path, "a", encoding="utf-8", newline="\n")
    try:
        handle.write(json.dumps(payload) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()


def run(args, root):
    output_path = getattr(args, "output", None)
    dev_probe = args.mode == "devprobe"

    def emit_tee(kind, **row):
        if dev_probe:
            row["dev_probe"] = True
        emit(kind, **row)
        record_row(output_path, {"type": kind, **row})

    repos = {name: getattr(args, name) for name in REPOS}
    editor_v2 = getattr(args, "editor_v2", False)
    arms_all = (*ARMS, EXCERPT_ARM) if editor_v2 else ARMS
    qualification_cases = QUALIFICATION_V2 if editor_v2 else QUALIFICATION
    inventory = (*CASES, *qualification_cases)
    if (len(CASES) != 24 or len({key(case) for case in CASES}) != 24
            or len({key(case) for case in inventory}) != 24 + len(qualification_cases)):
        raise ValueError("the frozen task inventory has changed")
    if args.mode == "recall":
        contexts(root, repos, excerpts=editor_v2)
        return
    grader = Grader(args.wasi, fuel=4_000_000_000)
    canary = root / "host-canary.txt"
    canary.write_text("outside grader", encoding="utf-8")
    grader.verify_boundary(canary)
    originals = {}
    for case in inventory:
        original = definition(case, source(case, repos))
        originals[key(case)] = original
        reference = original.splitlines()[0] + "\n" + indent(case.reference, "    ")
        assert grade(grader, case, original) == (False, True), key(case)
        assert grade(grader, case, reference) == (True, True), key(case)
    emit_tee("preflight", held_out=len(CASES), qualification=len(qualification_cases),
         qualification_set="v2" if editor_v2 else "frozen", repositories=REPOS)
    if args.mode == "preflight":
        return
    if args.mode == "regrade":
        rows = read_rows(args.input)
        cases = {key(case): case for case in inventory}
        selected = [row for row in rows if row["type"] in ("qualification", "scored")]
        for row in selected:
            case = cases[row["task"]]
            candidate = row["candidate"]
            if candidate is not None:
                assert function_only(candidate, case.symbol, originals[row["task"]]) == candidate
            changed, preserved = grade(grader, case, candidate)
            assert (changed and preserved, not preserved) == (row["passed"], row["regression"]), row["task"]
        emit_tee("regrade", checked=len(selected), **summarize(rows, arms_all))
        return
    tags = {row["name"]: row["digest"] for row in request("/api/tags")["models"]}
    fenced_editor = args.fenced_editor
    emit_tee("environment", model=args.model, model_digest=MODELS[args.model], num_gpu=0, context_tokens=2048,
         max_output_tokens=FENCED_OUTPUT if fenced_editor else MAX_OUTPUT, fenced_editor=fenced_editor,
         editor_v2=editor_v2, repair=editor_v2, excerpt_arm=EXCERPT_ARM if editor_v2 else None,
         seeds=SEEDS, ollama=request("/api/version")["version"],
         protocol_sha256=protocol_digest(),
         trace_revision=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    assert tags.get(args.model) == MODELS[args.model]
    if args.mode == "qualify":
        rows = []
        for case in qualification_cases:
            row = proposal(grader, case, originals[key(case)], args.model, SEEDS[0], "qualification", case.note,
                           fenced_editor=fenced_editor, repair=editor_v2)
            rows.append(row)
            emit_tee("qualification", **row)
        emit_tee("qualification_summary", model=args.model, passed=sum(r["passed"] for r in rows), required=len(rows))
        return
    if not dev_probe:
        qualified = read_rows(args.input)
        assert any(r["type"] == "environment" and r["model_digest"] == tags[args.model]
                   and r.get("protocol_sha256") == protocol_digest() and r.get("fenced_editor", False) == fenced_editor
                   and bool(r.get("editor_v2", False)) == editor_v2
                   for r in qualified)
        qualification = [r for r in qualified if r["type"] == "qualification" and r["model"] == args.model]
        assert len(qualification) == len(qualification_cases) and all(
            any(row["task"] == key(case) and row["passed"]
                and grade(grader, case, row["candidate"]) == (True, True) for row in qualification)
            for case in qualification_cases
        )
    memory = root / "memory"
    memory.mkdir()
    blocks = contexts(memory, repos, excerpts=editor_v2)
    if editor_v2:
        blocks, excerpts = blocks
    # Shards preserve identical per-task arm order and allow independent repeats.
    cases = CASES if args.repo is None else [case for case in CASES if case.repo == args.repo]
    rows = []
    for repeat in args.repeat or (range(1) if dev_probe else range(3)):
        seed = SEEDS[repeat]
        for case in cases:
            order = random_order(seed, key(case), arms_all)
            for arm in order:
                if arm == "trace":
                    context = blocks[key(case)]
                elif arm == EXCERPT_ARM:
                    context = excerpts[key(case)]
                elif arm == "notes":
                    context = case.note
                else:
                    context = ""
                row = proposal(grader, case, originals[key(case)], args.model, seed, arm, context,
                               fenced_editor=fenced_editor, repair=editor_v2)
                rows.append({"type": "scored", "dev_probe": dev_probe, **row})
                emit_tee("scored", **row)
    emit_tee("summary", **summarize(rows, arms_all))


def random_order(seed, task, arms=None):
    """Deterministic per-task arm order; pass the full arm tuple explicitly."""
    arms = ARMS if arms is None else arms
    return Random(f"{seed}:{task}").sample(arms, len(arms))


def main():
    sys.stdout.reconfigure(encoding="utf-8", write_through=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "recall", "qualify", "score", "regrade", "devprobe"))
    for name in REPOS:
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--wasi", type=Path)
    parser.add_argument("--model", choices=MODELS)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--repo", choices=REPOS)
    parser.add_argument("--repeat", type=int, choices=range(3), action="append")
    parser.add_argument("--fenced-editor", action="store_true")
    parser.add_argument("--editor-v2", action="store_true",
                        help="mechanical extraction fallback, one equal-budget retry, evidence-linked excerpt arm")
    parser.add_argument("--output", type=Path, help="append every emitted row to this JSONL file durably")
    args = parser.parse_args()
    if (not __debug__ or (args.mode != "recall" and args.wasi is None)
            or (args.mode in ("qualify", "score", "devprobe") and args.model is None)
            or (args.mode in ("score", "regrade") and args.input is None)
            or (args.repeat and len(set(args.repeat)) != len(args.repeat))
            or (args.editor_v2 and not args.fenced_editor)):
        parser.error("use assertions, provide WASI/model/input when required, select unique repeats, "
                     "and run editor v2 with the fenced editor")
    if args.mode == "devprobe" and (not args.editor_v2 or args.output is None or args.output.exists()):
        parser.error("devprobe requires editor v2 and a new output file")
    with TemporaryDirectory(prefix="trace-notes-", dir=args.scratch) as directory:
        run(args, Path(directory))


if __name__ == "__main__":
    main()
