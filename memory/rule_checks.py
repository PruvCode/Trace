"""Run the checks attached to approved rules, and diagnose failures.

Governing principle: TRACE remembers what happened, proposes what should be
remembered, and checks what has been explicitly approved. A check result is
evidence, not a verdict on correctness: TRACE reports, it never edits code, and
its guidance never tells an agent to bend code to fit a disputed rule.

Only approved rules are executed (see ``memory.rules``); proposed rules are
displayed, never run. Approved checks are reviewed code, like the project's
tests, so they run with the project's own interpreter and test runner, in a
subprocess with a timeout and a minimal environment.

A failing check is not automatically a stale rule. Each failure is rerun once
and classified:

- ``flaky``: failed, then passed on the rerun. It proves nothing either way, so
  the run is *inconclusive*, not verified.
- ``environment``: the check could not run (import error, missing runner,
  timeout, collection error). The rule is not in question, and it was not
  verified either: the run is *inconclusive*, never a silent pass.
- ``regression_in_progress``: the governed file has uncommitted changes. The
  rule stays approved; the change has to satisfy it or be proposed as a rule
  change.
- ``stale_or_regression``: fails deterministically on committed code. Only a
  person can decide whether the rule or the code is wrong.
- ``missing_check``: the approved pytest check no longer exists (the test was
  renamed or removed). Verification is inconclusive until its check is updated.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import file_digest
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
BLOCKING = ("regression_in_progress", "stale_or_regression")
INCONCLUSIVE = ("flaky", "environment", "missing_check")
_NOT_FOUND = ("ERROR: not found:", "no match in any of", "file or directory not found")

@dataclass(frozen=True)
class Execution:
    outcome: str
    output: str = ""
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0


Executor = Callable[[Rule, Check], Execution | tuple[str, str]]


@dataclass(frozen=True)
class CheckResult:
    rule: Rule
    check: Check
    outcome: str
    output: str
    classification: str | None = None  # set for failures only
    digest: str | None = None  # SHA-256 of the governed file when the check ran
    attempts: tuple[Execution, ...] = ()

    @property
    def identity(self) -> str:
        return f"{self.rule.key}:{self.check.kind}:{self.check.value}"

    def as_record(self) -> dict:
        return {"scope": self.rule.scope, "rule": self.rule.text, "kind": self.check.kind, "check": self.check.value,
                "outcome": self.outcome, "classification": self.classification, "output": self.output,
                "digest": self.digest, "attempts": [
                    {"outcome": attempt.outcome, "passed": attempt.passed, "failed": attempt.failed,
                     "skipped": attempt.skipped, "errors": attempt.errors}
                    for attempt in self.attempts
                ]}


def _environment() -> dict:
    keep = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "VIRTUAL_ENV")
    return {key: os.environ[key] for key in keep if key in os.environ}


def _tail(text: str) -> str:
    text = redact_mod.redact_text(text or "")
    return text if len(text) <= MAX_OUTPUT else "…" + text[-MAX_OUTPUT:]


_PY_PROBE = r"""
import importlib, importlib.util, inspect, json, sys
from pathlib import Path
root, path, expression = sys.argv[1:4]
target = Path(root, path)
try:
    # Inside a package, import by dotted name so relative imports work; otherwise load the file.
    parts, base = ([] if target.name == "__init__.py" else [target.stem]), target.parent
    while (base / "__init__.py").is_file():
        parts.insert(0, base.name)
        base = base.parent
    sys.path[:0] = [str(base), root]
    if parts and base != target.parent:
        module = importlib.import_module(".".join(parts))
    else:
        spec = importlib.util.spec_from_file_location("_trace_rule_target", target)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
except BaseException as exc:
    print(json.dumps({"outcome": "error", "output": f"could not import {path}: {type(exc).__name__}: {exc}"}))
    raise SystemExit(0)
try:
    value = eval(compile(expression, "<rule check>", "eval"), vars(module))
    if inspect.isawaitable(value) or inspect.isgenerator(value) or inspect.isasyncgen(value):
        if inspect.iscoroutine(value):
            value.close()
        print(json.dumps({"outcome": "error", "output": "check returned an unevaluated awaitable or generator"}))
    else:
        passed = bool(value)
        print(json.dumps({"outcome": "pass" if passed else "fail", "output": "" if passed else f"evaluated to {value!r}"}))
except ImportError as exc:
    print(json.dumps({"outcome": "error", "output": f"{type(exc).__name__}: {exc}"}))
except BaseException as exc:
    print(json.dumps({"outcome": "fail", "output": f"{type(exc).__name__}: {exc}"}))
"""


_PYTEST_PROBE = r"""
import json, os, sys
from collections import defaultdict
import pytest

class Capture:
    selected = set()
    reports = defaultdict(list)
    def pytest_collection_finish(self, session):
        self.selected = {item.nodeid for item in session.items}
    def pytest_runtest_logreport(self, report):
        self.reports[report.nodeid].append(report)

capture = Capture()
sys.path.insert(0, os.getcwd())
status = int(pytest.main(["-q", "-p", "no:cacheprovider", sys.argv[1]], plugins=[capture]))
counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
for node in capture.selected:
    reports = capture.reports[node]
    if any(report.failed for report in reports if report.when == "call"):
        counts["failed"] += 1
    elif any(report.failed for report in reports):
        counts["errors"] += 1
    elif any(report.skipped or hasattr(report, "wasxfail") for report in reports):
        counts["skipped"] += 1
    elif len(reports) == 3 and {report.when for report in reports} == {"setup", "call", "teardown"} and all(
            report.passed for report in reports):
        counts["passed"] += 1
    else:
        counts["errors"] += 1
if not capture.selected or (status != 0 and not counts["failed"] and not counts["errors"]):
    counts["errors"] += 1
print("TRACE_PYTEST_RESULT=" + json.dumps(counts))
raise SystemExit(status)
"""


def subprocess_executor(
    root: Path, python: str | None = None, *, pythonpath: tuple[str, ...] = (), detailed: bool = False,
) -> Executor:
    """Run ``py:`` checks against the governed file and ``pytest:`` checks with the project's runner.

    ``pythonpath`` lists repository-relative source roots (e.g. ``src``) to put first on
    ``PYTHONPATH``, so a src-layout project is tested from this tree, not an installed copy.
    """
    interpreter = python or os.environ.get("TRACE_RULES_PYTHON") or sys.executable
    environment = _environment()
    if pythonpath:
        environment["PYTHONPATH"] = os.pathsep.join(str(root / entry) for entry in pythonpath)
    inside = root.resolve()

    def execute(rule: Rule, check: Check) -> Execution:
        target = check.value.split("::")[0] if check.kind == "pytest" else rule.path
        if not (root / target).resolve().is_relative_to(inside):
            return Execution("error", f"{target} resolves outside the repository; not run", errors=1)
        if check.kind == "py":
            command = [interpreter, "-P", "-c", _PY_PROBE, str(root), rule.path, check.value]
        else:
            command = [interpreter, "-P", "-c", _PYTEST_PROBE, check.value]
        try:
            done = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True,
                                  timeout=TIMEOUT, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return Execution("error", f"timed out after {TIMEOUT} s", errors=1)
        except OSError as exc:
            return Execution("error", _tail(f"could not start {interpreter}: {exc}"), errors=1)
        lines = done.stdout.rstrip().splitlines()
        if check.kind == "py":
            try:
                result = json.loads(lines[-1])
                outcome = result["outcome"]
                if done.returncode != 0 or outcome not in OUTCOMES:
                    raise ValueError("invalid Python check result")
                return Execution(outcome, _tail(result["output"]), int(outcome == "pass"),
                                 int(outcome == "fail"), errors=int(outcome == "error"))
            except (IndexError, ValueError, KeyError, TypeError):
                return Execution("error", _tail(done.stderr or done.stdout or "no Python check result"), errors=1)
        if not lines or not lines[-1].startswith("TRACE_PYTEST_RESULT="):
            return Execution("error", _tail(done.stderr or done.stdout or "no pytest check result"), errors=1)
        output = _tail("\n".join(lines[:-1]) + "\n" + done.stderr)
        try:
            counts = json.loads(lines[-1].split("=", 1)[1])
            if not isinstance(counts, dict) or set(counts) != {"passed", "failed", "skipped", "errors"} or any(
                type(value) is not int or value < 0 for value in counts.values()
            ):
                raise ValueError("invalid pytest counts")
        except (TypeError, ValueError):
            return Execution("error", "invalid pytest check result", errors=1)
        if counts["failed"]:
            outcome = "fail"
        elif done.returncode == 0 and counts["passed"] and not (counts["skipped"] or counts["errors"]):
            outcome = "pass"
        else:
            outcome = "error"
            if not counts["skipped"] and not counts["errors"]:
                counts["errors"] = 1
        return Execution(outcome, output, **counts)

    def pair(rule: Rule, check: Check) -> tuple[str, str]:
        result = execute(rule, check)
        return result.outcome, result.output

    return execute if detailed else pair


def _uncommitted(root: Path, path: str) -> bool:
    """True when the governed file is untracked or differs from HEAD (git failure counts as changed)."""
    try:
        task_library.local_git(root, "ls-files", "--error-unmatch", "--", path)
        task_library.local_git(root, "diff", "--quiet", "HEAD", "--", path)
        return False
    except ValueError:
        return True


def _vanished(output: str, check: Check) -> bool:
    """pytest could not find the node itself (renamed or removed), as opposed to failing to collect its module."""
    return any(marker in output for marker in _NOT_FOUND) and "ERROR " + check.value.split("::")[0] not in output


def _digest(root: Path, path: str) -> str | None:
    try:
        target = root / path
        if not target.resolve().is_relative_to(root.resolve()) or not target.is_file():
            return None
        with target.open("rb") as handle:
            return file_digest(handle, "sha256").hexdigest()
    except (OSError, RuntimeError):
        return None


def run(root: Path, rules: list[Rule], executor: Executor) -> list[CheckResult]:
    """Execute every check of the given (approved) rules and classify failures."""
    results = []

    def execute(rule: Rule, check: Check) -> Execution:
        result = executor(rule, check)
        if isinstance(result, Execution):
            return result
        outcome, output = result
        if outcome not in OUTCOMES:
            return Execution("error", f"executor returned {outcome!r}", errors=1)
        return Execution(outcome, output, int(outcome == "pass"), int(outcome == "fail"),
                         errors=int(outcome == "error"))

    for rule in rules:
        digest = _digest(root, rule.path)
        for check in rule.checks:
            first = execute(rule, check)
            outcome, output = first.outcome, first.output
            attempts = (first,)
            classification = None
            if first.skipped and outcome == "error" and not first.errors:
                classification = "environment"
            elif outcome != "pass":
                second = execute(rule, check)
                attempts += (second,)
                if second.outcome == "pass":
                    classification = "flaky"
                elif "fail" in (outcome, second.outcome):
                    outcome = "fail"
                    output = first.output if first.outcome == "fail" else second.output
                    classification = "regression_in_progress" if _uncommitted(root, rule.path) else "stale_or_regression"
                elif check.kind == "pytest" and _vanished(output, check):
                    classification = "missing_check"
                else:
                    classification = "environment"
                    output = output or second.output
            results.append(CheckResult(rule, check, outcome, output, classification, digest, attempts))
    return results


def blocking(results: list[CheckResult]) -> list[CheckResult]:
    """Failures someone has to act on: code changes that break a rule, or rules that may be stale."""
    return [r for r in results if r.classification in BLOCKING]


def inconclusive(results: list[CheckResult]) -> list[CheckResult]:
    """Checks that neither passed nor failed reliably: they could not run, or gave different results on rerun."""
    return [r for r in results if r.classification in INCONCLUSIVE]


def verdict(results: list[CheckResult]) -> str:
    """One word for the run: failing beats inconclusive beats verified; nothing ran → 'nothing to check'."""
    if not results:
        return "nothing to check"
    return "failing" if blocking(results) else "inconclusive" if inconclusive(results) else "verified"


def changes_since(root: Path, ref: str | None, path: str, limit: int = 5) -> list[str]:
    """Commits touching ``path`` that are on HEAD but not on the approval ref (newest first)."""
    if not ref:
        return []
    try:
        log = task_library.local_git(root, "log", "--oneline", f"-{limit}", f"{ref}..HEAD", "--", path)
    except ValueError:
        return []
    return [line for line in log.decode("utf-8", "replace").splitlines() if line.strip()]


_NEXT = {  # guidance only: TRACE never edits code, and never asks for code to be bent to fit a disputed rule
    "flaky": "Failed, then passed on rerun: inconclusive, so this rule is unverified, not passed. Do not change code "
             "for it; report the check for review.",
    "environment": "The check could not run: inconclusive, so this rule is unverified, not passed. Fix the "
                   "environment and rerun; do not change code for it.",
    "regression_in_progress": "Your uncommitted change fails this approved rule's check. If the change is wrong, fix "
                              "it. If the rule is wrong for this change, do not bend the code to fit: propose a rule "
                              "change in .trace/RULES.md for review.",
    "stale_or_regression": "Committed code fails an approved rule. A person must decide whether the code or the "
                           "rule is wrong; do not edit code just to make this check pass.",
    "missing_check": "The approved check no longer exists (test renamed or removed), so this rule is not enforced. "
                     "Update the check in .trace/RULES.md through review.",
}


def next_step(result: CheckResult, *, repeated: bool = False, changes: list[str] | None = None) -> str:
    """Deterministic guidance for a failed check: what to do next and where deeper evidence is."""
    if result.classification is None:
        return ""
    steps = [_NEXT[result.classification]]
    if repeated:
        steps.append("The same file content already failed this check. Stop: do not retry the same change. "
                     "Escalate to a person, who decides whether the code or the rule is wrong.")
    if changes:
        steps.append("Commits touching this file since approval: " + "; ".join(changes))
    if result.rule.provenance and result.classification in BLOCKING:
        steps.append(f"Why the rule exists: {result.rule.provenance} (open with `trace evidence` or MCP get_evidence).")
    return " ".join(steps)
