"""Maintained notes + test-and-repair versus TRACE rules + executable checks + the same repair budget.

DEVELOPMENT PILOT ON PREVIOUSLY SEEN TASKS (the 24 frozen maintained-notes
tasks). Rows carry ``dev_probe: true``; nothing here is held-out evidence.

Arms (same model, prompt template, editor, budget and hidden grader):

  notes_repair   context = the maintained note. After each attempt the agent
                 runs the project's existing tests and repairs on failures.
  rules_text     context = the approved rule rendered by ``memory.rules`` (plain
                 sentences, with its checks listed as text). Repairs on the same
                 existing tests only; the checks are never executed. Diagnostic:
                 separates "seeing concrete checks" from "running them".
  rules_checks   same context as rules_text. After each attempt the agent runs the
                 existing tests AND the approved rule's checks through
                 ``memory.rule_checks``, and repairs on either.

Budget: at most ``MAX_CALLS`` model calls per task and arm (one first attempt
plus repairs). A mechanically invalid reply uses one call and gets the frozen
editor's format feedback. The loop stops early when every check the arm can run
passes. The hidden behavior and preservation checks are never shown.

Measured per run: first-attempt success, final success, final regression
(preservation failure), model calls, repairs used, wall time, input and
output tokens, plus every attempt's candidate, reply and hidden verdict.
"""

from __future__ import annotations

import argparse
import ast
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from secrets import token_hex
from tempfile import gettempdir
from time import perf_counter

from memory import rule_checks, rules
from research.phase5.grader import Grader, MAX_SOURCE
from research.preview import editor
from research.preview import notes_trial as nt
from research.preview.effectiveness import request as ollama
from research.preview.notes_cases import CASES, REPOS
from research.preview.repair_cases import CHECKS, TESTS

ARMS = ("notes_repair", "rules_text", "rules_checks")
MAX_CALLS = 3
SMOKE = ("trace:memory/store.py:clamp_limit", "click:src/click/formatting.py:iter_rows")
SAMPLED_SEEDS = (52183, 52189, 52193, 52199, 52201)


def sha(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def protocol_digest() -> str:
    files = [Path(__file__), Path(__file__).with_name("repair_cases.py"), Path(editor.__file__),
             Path(rules.__file__), Path(rule_checks.__file__)]
    return sha256(b"".join(path.read_bytes().replace(b"\r\n", b"\n") for path in files)).hexdigest()


def rules_markdown(case) -> str:
    checks = "".join(f"    - py: {check}\n" for check in CHECKS.get(nt.key(case), ()))
    return f"## {case.path} :: {case.symbol}\n- {case.note}\n" + (f"  checks:\n{checks}" if checks else "")


def rule_set(case) -> rules.RuleSet:
    if not case.history:
        return rules.RuleSet()
    parsed, errors = rules.parse(rules_markdown(case))
    assert not errors and len(parsed) == 1 and parsed[0].text == case.note, (case.symbol, errors)
    return rules.RuleSet(approved=parsed)


def context(case, arm: str) -> str:
    if arm == "notes_repair":
        return case.note
    return rules.render(rule_set(case), [case.path], [case.symbol])


def evaluate(grader, case, candidate: str, checks) -> list[tuple[str, str]]:
    """Run each check against the candidate in WASI. Returns [(check, 'pass' | failure description)]."""
    if not checks:
        return []
    marker = token_hex(12)
    probes = ", ".join(f"probe(lambda: {check}, {_left(check)})" for check in checks)
    # Like a test runner's assertion report: on a failed comparison, show what the left side produced.
    script = (f"from __future__ import annotations\nimport json\n{case.prelude}\n{nt.CHECK_HELPER}\n"
              "def probe(f, left=None):\n    try:\n        if f():\n            return 'pass'\n"
              "    except Exception as exc:\n        return f'raised {type(exc).__name__}: {exc}'[:200]\n"
              "    if left is None:\n        return 'evaluated to False'\n"
              "    try:\n        return f'evaluated to False; left side was {left()!r}'[:200]\n"
              "    except Exception as exc:\n        return f'evaluated to False; left side raised {type(exc).__name__}'\n"
              f"{candidate}\nprint({marker!r} + json.dumps([{probes}]))")
    ok, output = grader.run(script)
    text = output.decode("utf-8", errors="replace").strip()
    if not ok or not text.startswith(marker):
        return [(check, "could not run (the function failed to load or the check timed out)") for check in checks]
    return list(zip(checks, json.loads(text[len(marker):])))


def _left(check: str) -> str:
    """A lambda for the left operand of a single comparison, or None."""
    node = ast.parse(check, mode="eval").body
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.Is)):
        return f"lambda: {ast.unparse(node.left)}"
    return "None"


def wasi_executor(grader, case, candidate: str):
    def run(rule, check):
        [(_, result)] = evaluate(grader, case, candidate, [check.value])
        return ("pass", "") if result == "pass" else ("error" if result.startswith("could not run") else "fail", result)
    return run


def repair_message(prompt: str, previous: str, failures: list[tuple[str, str]], defect: str | None) -> str:
    if defect is not None:
        return f"{prompt}\n\nPrevious reply:\n{previous}\n\n{editor.retry_feedback(defect, '')}"
    listed = "\n".join(f"- {check}: {result}" for check, result in failures)
    return (f"{prompt}\n\nYour previous function:\n```python\n{previous}\n```\n\n"
            f"Running the project's checks on that function gave these failures:\n{listed}\n\n"
            "Fix the function so these checks pass while keeping the requested behavior. Respond with the complete "
            "updated Python function in a Python code block.")


def attempt_loop(grader, case, original, arm, seed, temperature, model):
    ctx = context(case, arm)
    prompt = (
        "Implement the user request by making the smallest code change. The project note gives the accepted rule "
        "for this change. Retain every part of the existing function that the rule does not change. Respond with "
        "the complete updated Python function in a Python code block.\n\n"
        f"User request: {case.request}\n"
        f"Project note: {ctx or '(none)'}\n\n"
        + (f"Supporting definitions:\n```python\n{case.prelude}\n```\n\n" if case.prelude else "")
        + f"```python\n{original}\n```"
    )
    tests = list(TESTS[nt.key(case)])
    approved = rule_set(case).approved if arm == "rules_checks" else []
    started = perf_counter()
    attempts, message, final = [], prompt, None
    stopped = "budget"
    for call in range(MAX_CALLS):
        t0 = perf_counter()
        try:
            reply = ollama("/api/chat", {
                "model": model, "stream": False, "keep_alive": "10m",
                "messages": [{"role": "user", "content": message}],
                "options": {"temperature": temperature, "seed": seed, "num_ctx": 4096,
                            "num_predict": nt.FENCED_OUTPUT, "num_gpu": 0},
            })
        except (OSError, RuntimeError) as exc:
            return None, {"infra_error": type(exc).__name__, "attempts": attempts}
        content = (reply.get("message") or {}).get("content") or ""
        candidate, defect = (None, "no-block") if reply.get("done_reason") == "length" else nt.function_candidate(content, case, original)
        if candidate is not None and len(candidate) > MAX_SOURCE:
            candidate, defect = None, "no-block"
        record = {"call": call + 1, "reply": content, "done_reason": reply.get("done_reason"), "candidate": candidate,
                  "defect": defect if candidate is None else None, "prompt_sha256": sha(message),
                  "input_tokens": reply.get("prompt_eval_count") or 0, "output_tokens": reply.get("eval_count") or 0}
        failures = []
        if candidate is not None:
            final = candidate
            changed, preserved = nt.grade(grader, case, candidate)
            record.update(hidden_passed=changed and preserved, hidden_regression=not preserved)
            failures = [(c, r) for c, r in evaluate(grader, case, candidate, tests) if r != "pass"]
            # The failure classification (git state) is irrelevant inside the sandbox; any non-repo root works.
            results = rule_checks.run(Path(gettempdir()), approved, wasi_executor(grader, case, candidate)) if approved else []
            failures += [(r.check.value, r.output or "evaluated to False") for r in results if r.outcome != "pass"]
            record.update(visible_failures=len(failures),
                          rule_checks_run=len(results), rule_checks_failed=sum(r.outcome != "pass" for r in results))
        record["ms"] = round((perf_counter() - t0) * 1000, 2)
        attempts.append(record)
        if candidate is not None and not failures:
            stopped = "checks_passed"
            break
        message = repair_message(prompt, candidate if candidate is not None else content, failures,
                                 None if candidate is not None else (defect or "no-block"))
    valid = [a for a in attempts if a["candidate"] is not None]
    final_changed, final_preserved = nt.grade(grader, case, final) if final is not None else (False, False)
    return {
        "task": nt.key(case), "arm": arm, "seed": seed, "temperature": temperature, "model": model,
        "context_sha256": sha(ctx), "context_chars": len(ctx), "first_prompt_sha256": sha(prompt),
        "first_attempt_passed": bool(attempts and attempts[0].get("hidden_passed")),
        "first_attempt_valid": bool(attempts and attempts[0]["candidate"] is not None),
        "final_candidate": final, "final_passed": final_changed and final_preserved,
        "final_regression": final is not None and not final_preserved, "valid_edit": final is not None,
        "model_calls": len(attempts), "repairs": max(0, len(attempts) - 1), "stopped": stopped,
        "ever_passed": any(a.get("hidden_passed") for a in valid),
        "latency_ms": round((perf_counter() - started) * 1000, 2),
        "input_tokens": sum(a["input_tokens"] for a in attempts), "output_tokens": sum(a["output_tokens"] for a in attempts),
        "attempts": attempts,
    }, None


def setup(args):
    repos = {name: getattr(args, name) for name in REPOS}
    grader = Grader(args.wasi, fuel=4_000_000_000)
    canary = args.scratch / "host-canary.txt"
    canary.write_text("outside grader", encoding="utf-8")
    grader.verify_boundary(canary)
    originals = {}
    for case in CASES:
        key = nt.key(case)
        original = nt.definition(case, nt.source(case, repos))
        reference = original.splitlines()[0] + "\n" + nt.indent(case.reference, "    ")
        assert nt.grade(grader, case, original) == (False, True), key
        assert nt.grade(grader, case, reference) == (True, True), key
        hidden = {ast.unparse(ast.parse(c, mode="eval")) for expr in (case.changed, case.preserved)
                  for c in _clauses(expr)}
        visible = [ast.unparse(ast.parse(c, mode="eval")) for c in (*TESTS[key], *CHECKS.get(key, ()))]
        assert not hidden.intersection(visible), f"visible check repeats a hidden clause: {key}"
        assert all(r == "pass" for _, r in evaluate(grader, case, original, TESTS[key])), f"test fails on original: {key}"
        assert all(r == "pass" for _, r in evaluate(grader, case, reference, TESTS[key])), f"test fails on reference: {key}"
        if case.history:
            on_reference = evaluate(grader, case, reference, CHECKS[key])
            on_original = evaluate(grader, case, original, CHECKS[key])
            assert all(r == "pass" for _, r in on_reference), (key, on_reference)
            assert any(r != "pass" for _, r in on_original), f"no rule check fails on the original: {key}"
        else:
            assert key not in CHECKS
        originals[key] = original
    return grader, originals


def _clauses(expression: str) -> list[str]:
    node = ast.parse(expression, mode="eval").body
    parts = node.values if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And) else [node]
    return [ast.unparse(part) for part in parts]


def run_key(row) -> tuple:
    return (row["task"], row["arm"], row["seed"], row["run"], row["temperature"], row["first_prompt_sha256"])


def deduplicated(rows):
    kept, duplicates = {}, 0
    for row in rows:
        key = run_key(row)
        if key in kept:
            first = kept[key]
            if (first["final_candidate"], first["final_passed"], first["model_calls"]) != (
                    row["final_candidate"], row["final_passed"], row["model_calls"]):
                raise ValueError(f"conflicting duplicate rows for {key}")
            duplicates += 1
            continue
        kept[key] = row
    return list(kept.values()), duplicates


def run(args):
    grader, originals = setup(args)
    tags = {row["name"]: row["digest"] for row in ollama("/api/tags")["models"]}
    assert tags.get(args.model) == nt.MODELS[args.model]
    done = set()
    if args.output.exists():  # resume: skip runs already scored under this configuration
        done = {run_key(row)[:5] for row in nt.read_rows(args.output) if row["type"] == "scored"}
    nt.record_row(args.output, {
        "type": "environment", "dev_probe": True, "experiment": "repair_trial", "run": args.run,
        "model": args.model, "model_digest": tags[args.model], "ollama": ollama("/api/version")["version"],
        "trace_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "protocol_sha256": protocol_digest(), "temperature": args.temperature, "seeds": args.seed,
        "max_calls": MAX_CALLS, "num_ctx": 4096, "max_output_tokens": nt.FENCED_OUTPUT, "arms": args.arm,
        "tasks": args.task or "all",
    })
    tasks = [case for case in CASES if not args.task or nt.key(case) in args.task]
    for seed in args.seed:
        for case in tasks:
            for arm in nt.random_order(seed, nt.key(case), tuple(args.arm)):
                if (nt.key(case), arm, seed, args.run, args.temperature) in done:
                    continue
                row, error = attempt_loop(grader, case, originals[nt.key(case)], arm, seed, args.temperature, args.model)
                if error is not None:
                    nt.record_row(args.output, {"type": "infra_error", "task": nt.key(case), "arm": arm, "seed": seed, **error})
                    continue
                row = {"type": "scored", "dev_probe": True, "run": args.run, **row}
                nt.record_row(args.output, row)
                print(json.dumps({"task": case.symbol, "arm": arm, "seed": seed, "first": row["first_attempt_passed"],
                                  "final": row["final_passed"], "regression": row["final_regression"],
                                  "calls": row["model_calls"], "s": round(row["latency_ms"] / 1000)}), flush=True)


def regrade(args):
    grader, _ = setup(args)
    cases = {nt.key(case): case for case in CASES}
    rows = [row for row in nt.read_rows(args.output) if row["type"] == "scored"]
    mismatches = []
    for row in rows:
        case = cases[row["task"]]
        for attempt in row["attempts"]:
            if attempt["candidate"] is not None:
                changed, preserved = nt.grade(grader, case, attempt["candidate"])
                if (changed and preserved, not preserved) != (attempt["hidden_passed"], attempt["hidden_regression"]):
                    mismatches.append((row["task"], row["arm"], row["seed"], attempt["call"]))
        final = nt.grade(grader, case, row["final_candidate"]) if row["final_candidate"] else (False, False)
        if (final[0] and final[1]) != row["final_passed"]:
            mismatches.append((row["task"], row["arm"], row["seed"], "final"))
    report = {"type": "regrade", "dev_probe": True, "rows": len(rows), "mismatches": mismatches,
              "attempts_checked": sum(a["candidate"] is not None for r in rows for a in r["attempts"])}
    nt.record_row(args.regrade_output, report)
    print(json.dumps(report))
    assert not mismatches


def summarize(args):
    rows, duplicates = deduplicated([row for row in nt.read_rows(args.output) if row["type"] == "scored"])
    print(f"distinct runs {len(rows)}; identical duplicate rows collapsed {duplicates}")
    from statistics import median
    for run_name in sorted({row["run"] for row in rows}):
        print(f"\n== run {run_name}")
        print(f"{'arm':14} {'n':>4} {'first':>6} {'final':>6} {'ever':>5} {'regr':>5} {'valid':>6} {'calls':>6} "
              f"{'repairs':>8} {'med_s':>6} {'med_in':>7} {'med_out':>8}")
        for arm in ARMS:
            sample = [r for r in rows if r["run"] == run_name and r["arm"] == arm]
            if not sample:
                continue
            print(f"{arm:14} {len(sample):>4} {sum(r['first_attempt_passed'] for r in sample):>6} "
                  f"{sum(r['final_passed'] for r in sample):>6} {sum(r['ever_passed'] for r in sample):>5} "
                  f"{sum(r['final_regression'] for r in sample):>5} {sum(r['valid_edit'] for r in sample):>6} "
                  f"{sum(r['model_calls'] for r in sample):>6} {sum(r['repairs'] for r in sample):>8} "
                  f"{median(r['latency_ms'] for r in sample) / 1000:>6.0f} {median(r['input_tokens'] for r in sample):>7.0f} "
                  f"{median(r['output_tokens'] for r in sample):>8.0f}")
        by = {(r["task"], r["arm"], r["seed"]): r for r in rows if r["run"] == run_name}
        for left, right in (("rules_checks", "notes_repair"), ("rules_checks", "rules_text"), ("rules_text", "notes_repair")):
            pairs = [(by[t, left, s], by[t, right, s]) for (t, a, s) in by if a == left and (t, right, s) in by]
            gain = sorted({a["task"].split(":")[-1] for a, b in pairs if a["final_passed"] and not b["final_passed"]})
            loss = sorted({a["task"].split(":")[-1] for a, b in pairs if b["final_passed"] and not a["final_passed"]})
            print(f"  {left} vs {right}: final +{len(gain)} {gain}  -{len(loss)} {loss}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", write_through=True)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("preflight", "run", "regrade", "summary", "contexts"))
    for name in REPOS:
        parser.add_argument(f"--{name}", type=Path)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--wasi", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--regrade-output", type=Path)
    parser.add_argument("--model", default=nt.QWEN, choices=nt.MODELS)
    parser.add_argument("--run", default="greedy")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, action="append")
    parser.add_argument("--task", action="append", choices=[nt.key(case) for case in CASES])
    parser.add_argument("--arm", action="append", choices=ARMS)
    args = parser.parse_args()
    if not __debug__:
        parser.error("assertions are part of the protocol; do not run with -O")
    args.seed = args.seed or [nt.SEEDS[0]]
    args.arm = args.arm or list(ARMS)
    if args.mode == "preflight":
        setup(args)
        print(json.dumps({"preflight": "ok", "tasks": len(CASES), "protocol_sha256": protocol_digest()}))
    elif args.mode == "contexts":
        for case in CASES:
            if not args.task or nt.key(case) in args.task:
                for arm in ARMS:
                    print(f"===== {case.symbol} / {arm}\n{context(case, arm)}\n")
    elif args.mode == "run":
        run(args)
    elif args.mode == "regrade":
        regrade(args)
    else:
        summarize(args)


if __name__ == "__main__":
    main()
