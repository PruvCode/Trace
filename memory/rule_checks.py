"""Run the checks attached to approved rules, and diagnose failures.

Only approved rules are executed (see ``memory.rules``); proposed rules are
displayed, never run. Approved checks are reviewed code, like the project's
tests, so they run with the project's own interpreter and test runner, in a
subprocess with a timeout and a minimal environment.

A failing check is not automatically a stale rule. Each failure is rerun once
and classified:

- ``flaky``: passed on the rerun.
- ``environment``: the check could not run (import error, missing runner,
  timeout, collection error). The rule is not in question.
- ``regression_in_progress``: the governed file has uncommitted changes. The
  rule stays approved; the change has to satisfy it or be proposed as a rule
  change.
- ``stale_or_regression``: fails deterministically on committed code. Only a
  person can decide whether the rule or the code is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable

from memory import redact as redact_mod
from memory import task_library
from memory.rules import Check, Rule

TIMEOUT = 120
MAX_OUTPUT = 2000
OUTCOMES = ("pass", "fail", "error")

# An executor runs one check and returns (outcome, output); outcome is one of OUTCOMES.
Executor = Callable[[Rule, Check], tuple[str, str]]


@dataclass(frozen=True)
class CheckResult:
    rule: Rule
    check: Check
    outcome: str
    output: str
    classification: str | None = None  # set for failures only

    def as_record(self) -> dict:
        return {"scope": self.rule.scope, "rule": self.rule.text, "kind": self.check.kind, "check": self.check.value,
                "outcome": self.outcome, "classification": self.classification, "output": self.output}


def _environment() -> dict:
    keep = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "VIRTUAL_ENV")
    return {key: os.environ[key] for key in keep if key in os.environ}


def _tail(text: str) -> str:
    text = redact_mod.redact_text(text or "")
    return text if len(text) <= MAX_OUTPUT else "…" + text[-MAX_OUTPUT:]


_PY_PROBE = r"""
import importlib.util, json, sys
root, path, expression = sys.argv[1:4]
sys.path.insert(0, root)
try:
    spec = importlib.util.spec_from_file_location("_trace_rule_target", f"{root}/{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except BaseException as exc:
    print(json.dumps({"outcome": "error", "output": f"could not import {path}: {type(exc).__name__}: {exc}"}))
    raise SystemExit(0)
try:
    value = eval(compile(expression, "<rule check>", "eval"), vars(module))
    print(json.dumps({"outcome": "pass" if value else "fail", "output": "" if value else f"evaluated to {value!r}"}))
except BaseException as exc:
    print(json.dumps({"outcome": "fail", "output": f"{type(exc).__name__}: {exc}"}))
"""


def subprocess_executor(root: Path, python: str | None = None) -> Executor:
    """Run ``py:`` checks against the governed file and ``pytest:`` checks with the project's runner."""
    interpreter = python or os.environ.get("TRACE_RULES_PYTHON") or sys.executable

    def run(rule: Rule, check: Check) -> tuple[str, str]:
        if check.kind == "py":
            command = [interpreter, "-c", _PY_PROBE, str(root), rule.path, check.value]
        else:
            command = [interpreter, "-m", "pytest", "-q", "-p", "no:cacheprovider", check.value]
        try:
            done = subprocess.run(command, cwd=root, env=_environment(), capture_output=True, text=True,
                                  timeout=TIMEOUT, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return "error", f"timed out after {TIMEOUT} s"
        except OSError as exc:
            return "error", f"could not start {interpreter}: {exc}"
        if check.kind == "py":
            try:
                result = json.loads(done.stdout.strip().splitlines()[-1])
                return result["outcome"], _tail(result["output"])
            except (IndexError, ValueError, KeyError, TypeError):
                return "error", _tail(done.stderr or done.stdout)
        output = _tail((done.stdout or "") + (done.stderr or ""))
        return ("pass" if done.returncode == 0 else "fail" if done.returncode == 1 else "error"), output

    return run


def _uncommitted(root: Path, path: str) -> bool:
    """True when the governed file is untracked or differs from HEAD (git failure counts as changed)."""
    try:
        task_library.local_git(root, "ls-files", "--error-unmatch", "--", path)
        task_library.local_git(root, "diff", "--quiet", "HEAD", "--", path)
        return False
    except ValueError:
        return True


def run(root: Path, rules: list[Rule], executor: Executor) -> list[CheckResult]:
    """Execute every check of the given (approved) rules and classify failures."""
    results = []
    for rule in rules:
        for check in rule.checks:
            outcome, output = executor(rule, check)
            if outcome not in OUTCOMES:
                outcome, output = "error", f"executor returned {outcome!r}"
            classification = None
            if outcome != "pass":
                again, again_output = executor(rule, check)
                if again == "pass":
                    classification = "flaky"
                elif "error" in (outcome, again):
                    classification = "environment"
                    output = output or again_output
                elif _uncommitted(root, rule.path):
                    classification = "regression_in_progress"
                else:
                    classification = "stale_or_regression"
            results.append(CheckResult(rule, check, outcome, output, classification))
    return results


def blocking(results: list[CheckResult]) -> list[CheckResult]:
    """Failures someone has to act on: code changes that break a rule, or rules that may be stale."""
    return [r for r in results if r.classification in ("regression_in_progress", "stale_or_regression")]
