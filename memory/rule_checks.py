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
from hashlib import sha256
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

# An executor runs one check and returns (outcome, output); outcome is one of OUTCOMES.
Executor = Callable[[Rule, Check], tuple[str, str]]


@dataclass(frozen=True)
class CheckResult:
    rule: Rule
    check: Check
    outcome: str
    output: str
    classification: str | None = None  # set for failures only
    digest: str | None = None  # SHA-256 of the governed file when the check ran

    @property
    def identity(self) -> str:
        return f"{self.rule.key}:{self.check.kind}:{self.check.value}"

    def as_record(self) -> dict:
        return {"scope": self.rule.scope, "rule": self.rule.text, "kind": self.check.kind, "check": self.check.value,
                "outcome": self.outcome, "classification": self.classification, "output": self.output,
                "digest": self.digest}


def _environment() -> dict:
    keep = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "VIRTUAL_ENV")
    return {key: os.environ[key] for key in keep if key in os.environ}


def _tail(text: str) -> str:
    text = redact_mod.redact_text(text or "")
    return text if len(text) <= MAX_OUTPUT else "…" + text[-MAX_OUTPUT:]


_PY_PROBE = r"""
import importlib, importlib.util, json, sys
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
    print(json.dumps({"outcome": "pass" if value else "fail", "output": "" if value else f"evaluated to {value!r}"}))
except BaseException as exc:
    print(json.dumps({"outcome": "fail", "output": f"{type(exc).__name__}: {exc}"}))
"""


def subprocess_executor(root: Path, python: str | None = None, *, pythonpath: tuple[str, ...] = ()) -> Executor:
    """Run ``py:`` checks against the governed file and ``pytest:`` checks with the project's runner.

    ``pythonpath`` lists repository-relative source roots (e.g. ``src``) to put first on
    ``PYTHONPATH``, so a src-layout project is tested from this tree, not an installed copy.
    """
    interpreter = python or os.environ.get("TRACE_RULES_PYTHON") or sys.executable
    environment = _environment()
    if pythonpath:
        environment["PYTHONPATH"] = os.pathsep.join(str(root / entry) for entry in pythonpath)
    inside = root.resolve()

    def run(rule: Rule, check: Check) -> tuple[str, str]:
        target = check.value.split("::")[0] if check.kind == "pytest" else rule.path
        if not (root / target).resolve().is_relative_to(inside):
            return "error", f"{target} resolves outside the repository; not run"
        if check.kind == "py":
            command = [interpreter, "-c", _PY_PROBE, str(root), rule.path, check.value]
        else:
            command = [interpreter, "-m", "pytest", "-q", "-p", "no:cacheprovider", check.value]
        try:
            done = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True,
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


def _digest(root: Path, path: str) -> str | None:
    try:
        return sha256((root / path).read_bytes()).hexdigest()
    except OSError:
        return None


def run(root: Path, rules: list[Rule], executor: Executor) -> list[CheckResult]:
    """Execute every check of the given (approved) rules and classify failures."""
    results = []
    for rule in rules:
        digest = _digest(root, rule.path)
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
            results.append(CheckResult(rule, check, outcome, output, classification, digest))
    return results


def blocking(results: list[CheckResult]) -> list[CheckResult]:
    """Failures someone has to act on: code changes that break a rule, or rules that may be stale."""
    return [r for r in results if r.classification in BLOCKING]


def changes_since(root: Path, ref: str | None, path: str, limit: int = 5) -> list[str]:
    """Commits touching ``path`` that are on HEAD but not on the approval ref (newest first)."""
    if not ref:
        return []
    try:
        log = task_library.local_git(root, "log", "--oneline", f"-{limit}", f"{ref}..HEAD", "--", path)
    except ValueError:
        return []
    return [line for line in log.decode("utf-8", "replace").splitlines() if line.strip()]


_NEXT = {
    "flaky": "Passed on rerun. Do not change code for it; report the flaky check.",
    "environment": "The check could not run, so it says nothing about the rule. Fix the environment and rerun; "
                   "do not change code for it.",
    "regression_in_progress": "Your uncommitted change does not satisfy this approved rule. Fix the code, or "
                              "propose a rule change in .trace/RULES.md for review.",
    "stale_or_regression": "Committed code fails an approved rule. A person must decide whether the code or the "
                           "rule is wrong.",
}


def next_step(result: CheckResult, *, repeated: bool = False, changes: list[str] | None = None) -> str:
    """Deterministic guidance for a failed check: what to do next and where deeper evidence is."""
    if result.classification is None:
        return ""
    steps = [_NEXT[result.classification]]
    if repeated:
        steps.append("The same file content already failed this check. Change the approach or ask a person; "
                     "resubmitting it will not help.")
    if changes:
        steps.append("Commits touching this file since approval: " + "; ".join(changes))
    if result.rule.provenance and result.classification in BLOCKING:
        steps.append(f"Why the rule exists: {result.rule.provenance} (open with `trace evidence` or MCP get_evidence).")
    return " ".join(steps)
