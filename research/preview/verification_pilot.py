"""Compare a bounded test-and-repair workflow with OpenAI Evals."""

from __future__ import annotations

import argparse
from contextlib import chdir
import json
import os
from pathlib import Path
from secrets import token_hex
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

from research.phase5.grader import MAX_SOURCE, Grader
from research.preview import notes_trial as trial

SYMBOLS = ("clamp_limit", "iter_rows", "_apply_budgets")
OUTPUT_TOKENS = 380
CONTEXT_TOKENS = 2048
MAX_PROBE_CHARS = 768


def decode_draft(reply, case, original):
    """Accept a matching replacement and at most two literal-input checks."""
    try:
        payload = json.loads(reply)
    except (TypeError, ValueError, RecursionError):
        return None, []
    if not isinstance(payload, dict):
        return None, []
    source = payload.get("function")
    candidate = None
    if isinstance(source, str) and len(source) <= MAX_SOURCE:
        candidate, _ = trial.function_candidate(source, case, original)
    raw = payload.get("checks")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
        return candidate, []
    checks = []
    for check in raw:
        if (not isinstance(check, dict) or set(check) != {"args", "expected"}
                or not isinstance(check["args"], list) or len(check["args"]) > 3):
            continue
        try:
            if len(json.dumps(check, allow_nan=False)) <= MAX_PROBE_CHARS:
                checks.append(check)
        except (ValueError, RecursionError):
            continue
    return candidate, checks


def run_check(grader, case, candidate, check):
    """Run an agent's literal input in the same bounded WASI guest as grading."""
    if candidate is None:
        return {"error": "No valid replacement function"}
    args = "args"
    if case.symbol == "iter_rows":
        args = "[[tuple(row) for row in args[0]], *args[1:]]"
    call = f"{case.symbol}(*{args})"
    if case.symbol == "iter_rows":
        call = f"list({call})"
    encoded = json.dumps(check, allow_nan=False)
    marker = token_hex(12)
    script = f"""from __future__ import annotations
import json
{case.prelude}
{candidate}
check = json.loads({encoded!r})
args = check["args"]
try:
    actual = {call}
    print({marker!r}, json.dumps({{"actual": actual}}, allow_nan=False))
except Exception as exc:
    print({marker!r}, json.dumps({{"error": type(exc).__name__}}))
"""
    ok, out = grader.run(script)
    prefix = f"{marker} ".encode()
    if ok and out.startswith(prefix):
        try:
            observed = json.loads(out[len(prefix):])
            if isinstance(observed, dict) and set(observed) in ({"actual"}, {"error"}):
                return observed
        except (ValueError, RecursionError):
            pass
    return {"error": "Check did not return bounded JSON"}


def check_results(grader, case, candidate, checks):
    results = []
    for check in checks:
        observed = run_check(grader, case, candidate, check)
        passed = ("actual" in observed
                  and json.dumps(observed["actual"], sort_keys=True) == json.dumps(check["expected"], sort_keys=True))
        results.append({**check, **observed, "passed": passed})
    return results


def verify_sample(grader, case, original, context, complete):
    started = perf_counter()
    task = (
        f"Task: {case.request}\nFile: {case.path}\n"
        f"Project note: {context or '(none)'}\n\n"
        f"Current source:\n```python\n{case.prelude}\n{original}\n```\n\n"
    )
    draft = complete(task + (
        "Implement the approved rule in a complete replacement function with the same signature. "
        "Derive one or two concrete checks for changed behavior from the task and relevant project note. "
        'Return a JSON object with "function" containing the source and "checks" containing objects '
        'with "args" (positional arguments) and "expected" (the expected return value). '
        "Use JSON values only, with arrays for tuples and lists. Preserve behavior outside the change."
    ), json_mode=True)
    initial, checks = decode_draft(draft, case, original)
    before = check_results(grader, case, initial, checks)
    final, after = initial, before
    retry = initial is None or not before or not all(row["passed"] for row in before)
    if retry:
        feedback = json.dumps(before, ensure_ascii=True)
        repair = complete(
            f"{task}"
            f"First replacement:\n```python\n{initial or '(unavailable)'}\n```\n"
            f"Results of your own checks: {feedback}\n"
            "Repair the function against the approved rule and your checks. "
            "Return only the complete replacement in a Python code block with the original signature.",
            json_mode=False,
        )
        if isinstance(repair, str) and len(repair) <= MAX_SOURCE:
            candidate, _ = trial.function_candidate(repair, case, original)
            if candidate is not None:
                rerun = check_results(grader, case, candidate, checks)
                if initial is None or sum(row["passed"] for row in rerun) >= sum(row["passed"] for row in before):
                    final, after = candidate, rerun

    # Hidden verdicts are computed after the repair decision.
    initial_changed, initial_preserved = trial.grade(grader, case, initial)
    changed, preserved = trial.grade(grader, case, final) if final != initial else (initial_changed, initial_preserved)
    return dict(
        initial=initial, candidate=final, checks_before=before, checks_after=after,
        retried=retry, initial_passed=initial_changed and initial_preserved, passed=changed and preserved,
        regression=final is not None and not preserved, elapsed_ms=round((perf_counter() - started) * 1000),
    )


def run_evals(args, grader, samples):
    # Evals creates an SDK client when it imports its registry.
    with patch.dict(os.environ, OPENAI_API_KEY="trace-local", OPENAI_BASE_URL="http://127.0.0.1:11434/v1"):
        from evals import Eval
        from evals.base import RunSpec
        from evals.record import LocalRecorder

    name = "trace_verification.dev.v1"
    class VerificationEval(Eval):
        def eval_sample(self, sample, rng):
            case, arm, original, context = sample
            tokens = [0, 0]

            def complete(prompt, *, json_mode):
                reply = trial.request("/api/chat", {
                    "model": args.model, "stream": False, "keep_alive": "10m",
                    **({"format": "json"} if json_mode else {}),
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"seed": trial.SEEDS[0], "temperature": 0, "num_ctx": CONTEXT_TOKENS,
                                "num_predict": OUTPUT_TOKENS, "num_gpu": 0},
                }, timeout=360)
                tokens[0] += reply.get("prompt_eval_count") or 0
                tokens[1] += reply.get("eval_count") or 0
                return reply["message"]["content"] if reply.get("done_reason") != "length" else ""

            row = verify_sample(grader, case, original, context, complete)
            row.update(task=trial.key(case), arm=arm, input_tokens=tokens[0], output_tokens=tokens[1])
            recorder.record_match(row.pop("passed"), **row)
            recorder.flush_events()
            return row

        def run(self, recorder):
            self.eval_all_samples(recorder, samples)
            rows = [event.data for event in recorder.get_events("match")]
            report = {}
            for arm in trial.ARMS:
                arm_rows = [row for row in rows if row["arm"] == arm]
                report[arm] = {
                    "samples": len(arm_rows),
                    "initial_passed": sum(row["initial_passed"] for row in arm_rows),
                    "passed": sum(row["correct"] for row in arm_rows),
                    "retries": sum(row["retried"] for row in arm_rows),
                    "regressions": sum(row["regression"] for row in arm_rows),
                }
            recorder.record_final_report(report)
            return report

    output = args.output.resolve()
    with output.open("xb"):
        pass
    # BlobFile interprets a Windows drive prefix as a URL scheme.
    with chdir(output.parent), patch.dict(os.environ, EVALS_SEQUENTIAL="1", EVALS_THREADS="1"):
        recorder = LocalRecorder(output.name, RunSpec(
            completion_fns=[args.model], eval_name=name, base_eval="trace_verification", split="dev",
            run_config=dict(model_digest=trial.MODELS[args.model], seed=trial.SEEDS[0],
                            context_tokens=CONTEXT_TOKENS, output_tokens=OUTPUT_TOKENS, max_calls=2,
                            repositories=trial.REPOS, reused_tasks=True),
            created_by="trace-local",
        ))
        return VerificationEval([], Path(__file__).parent, name=name).run(recorder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for repo in trial.REPOS:
        parser.add_argument(f"--{repo}", required=True, type=Path)
    parser.add_argument("--wasi", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default=trial.QWEN, choices=trial.MODELS)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("--output must name a new file")
    tags = {row["name"]: row["digest"] for row in trial.request("/api/tags")["models"]}
    if tags.get(args.model) != trial.MODELS[args.model]:
        parser.error("local model is missing or has a different digest")

    grader = Grader(args.wasi, fuel=4_000_000_000)
    repos = {repo: getattr(args, repo) for repo in trial.REPOS}
    with TemporaryDirectory(prefix="trace-verify-", dir=args.scratch) as directory:
        root = Path(directory)
        canary = root / "host-canary.txt"
        canary.write_text("outside grader", encoding="utf-8")
        grader.verify_boundary(canary)
        blocks = trial.contexts(root, repos)
        samples = []
        cases = [case for case in trial.CASES if case.symbol in SYMBOLS]
        if sorted(case.symbol for case in cases) != sorted(SYMBOLS):
            raise ValueError("the verification pilot expects its three named fixtures")
        for case in cases:
            original = trial.definition(case, trial.source(case, repos))
            reference = original.splitlines()[0] + "\n" + trial.indent(case.reference, "    ")
            if trial.grade(grader, case, original) != (False, True) or trial.grade(grader, case, reference) != (True, True):
                raise ValueError(f"fixture failed preflight: {trial.key(case)}")
            contexts = {"trace": blocks[trial.key(case)], "notes": case.note, "no_memory": ""}
            samples.extend((case, arm, original, contexts[arm]) for arm in trial.ARMS)
    print(json.dumps(run_evals(args, grader, samples), indent=2))


if __name__ == "__main__":
    main()
