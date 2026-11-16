"""Turn a real correction (a fix commit) into a proposed rule with a validated executable check.

A correction is a commit that changes non-test Python code and adds or changes
tests. TRACE drafts a rule from it and validates every check against history
before anyone is asked to approve it:

- **fails before**: the check *fails* (an assertion, not a collection or import
  error) on the parent revision with the fix commit's test files present, so a
  new test does not count as failing merely because it did not exist yet;
- **passes after**: it passes on the fix revision;
- **no false alarm**: it passes on later commits that touch the governed file
  (up to ``later``). A later failure is reported, never hidden: it is either a
  false alarm or a deliberate behavior change, which only a person can tell.

Checks that do not fail before or do not pass after are dropped and listed.
Nothing here approves anything: the result is a *proposed* rule plus a report.
Approval stays a reviewed merge of ``.trace/RULES.md`` to the default branch.

The drafter is deterministic: the rule's checks are the tests the fix commit
added or changed, its scope is the changed source function, and its text is the
commit subject, which a reviewer is expected to reword. Generating checks for
corrections that came without tests is a separate, measured step.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from memory import rule_checks
from memory import rules as rules_mod
from memory import task_library

GIT_TIMEOUT = 120
_PREFIX = re.compile(r"^(?:fix|bug|bugfix|hotfix)(?:\([^)]*\))?\s*[:!-]\s*", re.I)
_ISSUE_SUFFIX = re.compile(r"\s*\((?:#|gh-)\d+\)\s*$")


def _git(root: Path, *args: str) -> str:
    return task_library.local_git(root, *args, timeout=GIT_TIMEOUT).decode("utf-8", "replace")


def is_test_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return path.endswith(".py") and ("tests" in PurePosixPath(path).parts or "test" in PurePosixPath(path).parts
                                     or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py")


@dataclass
class Correction:
    sha: str
    parent: str
    subject: str
    source_files: list[str]
    test_files: list[str]


def correction(root: Path, sha: str) -> Correction:
    """Describe a fix commit. Raises ValueError for merges or commits without both code and tests."""
    full = _git(root, "rev-parse", "--verify", f"{sha}^{{commit}}").strip()
    parents = _git(root, "rev-list", "--parents", "-n", "1", full).split()[1:]
    if len(parents) != 1:
        raise ValueError(f"{sha} is not a single-parent commit")
    changed = [line.split("\t") for line in _git(root, "diff", "--name-status", "--no-renames", parents[0], full).splitlines()]
    paths = [parts[-1] for parts in changed if parts and parts[0][:1] in "AM" and parts[-1].endswith(".py")]
    tests = [p for p in paths if is_test_path(p)]
    sources = [p for p in paths if not is_test_path(p)]
    if not tests or not sources:
        raise ValueError(f"{sha} does not change both non-test code and tests")
    subject = _git(root, "log", "-1", "--format=%s", full).strip()
    return Correction(full, parents[0], subject, sources, tests)


def _show(root: Path, rev: str, path: str) -> str | None:
    try:
        return _git(root, "show", f"{rev}:{path}")
    except ValueError:
        return None


def _definitions(source: str | None) -> dict[str, str]:
    """Qualified name -> source text of top-level functions and class methods."""
    if not source:
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = ast.get_source_segment(source, node) or ""
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found[f"{node.name}.{item.name}"] = ast.get_source_segment(source, item) or ""
    return found


def changed_definitions(root: Path, item: Correction, path: str) -> list[str]:
    before, after = _definitions(_show(root, item.parent, path)), _definitions(_show(root, item.sha, path))
    return [name for name, text in after.items() if before.get(name) != text]


def changed_tests(root: Path, item: Correction) -> list[str]:
    """pytest node IDs of test functions the fix commit added or changed."""
    nodes = []
    for path in item.test_files:
        for name in changed_definitions(root, item, path):
            leaf = name.split(".")[-1]
            if leaf.startswith("test") and (name.count(".") == 0 or name.split(".")[0].startswith("Test")):
                nodes.append(f"{path}::{name.replace('.', '::')}")
    return nodes


def _rule_text(subject: str) -> str:
    text = _ISSUE_SUFFIX.sub("", _PREFIX.sub("", subject)).strip().rstrip(".")
    return (text[:1].upper() + text[1:] + ".")[: rules_mod.MAX_RULE_CHARS] if text else subject[: rules_mod.MAX_RULE_CHARS]


def draft(root: Path, item: Correction) -> rules_mod.Rule:
    """Deterministic draft: changed tests become checks, scoped to the changed source function."""
    nodes = changed_tests(root, item)
    if not nodes:
        raise ValueError(f"{item.sha[:12]} adds or changes no test function")
    # Scope to the first source file the fix changed; a reviewer sees every changed file in the report.
    changes = [(path, changed_definitions(root, item, path)) for path in item.source_files]
    path, names = next(((path, names) for path, names in changes if names), changes[0])
    symbol = names[0] if len(names) == 1 else None
    checks = tuple(rules_mod.Check("pytest", node) for node in nodes[: rules_mod.MAX_CHECKS])
    return rules_mod.Rule(path, symbol, _rule_text(item.subject), checks, f"trace:correction={item.sha[:12]}")


@dataclass
class Report:
    correction: dict
    rule: dict
    kept: list[str] = field(default_factory=list)
    dropped: dict[str, str] = field(default_factory=dict)  # check -> reason
    runs: list[dict] = field(default_factory=list)  # {"revision", "role", "check", "outcome", "output"}
    later_revisions: list[str] = field(default_factory=list)
    later_failures: list[dict] = field(default_factory=list)
    supporting_events: list[int] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return bool(self.kept)


@contextmanager
def _worktree(root: Path, scratch: Path | None):
    directory = Path(tempfile.mkdtemp(prefix="trace-guardrail-", dir=scratch))
    target = directory / "tree"
    _git(root, "worktree", "add", "--quiet", "--detach", str(target), "HEAD")
    try:
        yield target
    finally:
        try:
            _git(root, "worktree", "remove", "--force", str(target))
        finally:
            shutil.rmtree(directory, ignore_errors=True)


def validate(root: Path, item: Correction, rule: rules_mod.Rule, *, python: str | None = None,
             pythonpath: tuple[str, ...] = (), later: int = 5, scratch: Path | None = None) -> Report:
    report = Report(asdict(item), {"path": rule.path, "symbol": rule.symbol, "text": rule.text,
                                   "checks": [c.value for c in rule.checks], "provenance": rule.provenance})
    with _worktree(root, scratch) as tree:
        run = rule_checks.subprocess_executor(tree, python, pythonpath=pythonpath)

        def at(revision: str, role: str, checks, *, overlay_tests: bool = False) -> dict[str, str]:
            _git(tree, "checkout", "--quiet", "--force", "--detach", revision)
            _git(tree, "clean", "-qfdx", "-e", ".venv")
            if overlay_tests:
                _git(tree, "checkout", item.sha, "--", *item.test_files)
            outcomes = {}
            for check in checks:
                outcome, output = run(rule, check)
                if outcome == "error":  # say why it could not run: the test is gone, or the environment
                    outcome = "missing_check" if rule_checks._vanished(output, check) else "environment"
                outcomes[check.value] = outcome
                report.runs.append({"revision": revision, "role": role, "check": check.value,
                                    "outcome": outcome, "output": output[-600:]})
            return outcomes

        before = at(item.parent, "before", rule.checks, overlay_tests=True)
        failing = [c for c in rule.checks if before[c.value] == "fail"]
        for check in rule.checks:
            if before[check.value] != "fail":
                report.dropped[check.value] = f"does not fail before the fix ({before[check.value]})"
        after = at(item.sha, "after", failing) if failing else {}
        kept = [c for c in failing if after.get(c.value) == "pass"]
        for check in failing:
            if after.get(check.value) != "pass":
                report.dropped[check.value] = f"does not pass after the fix ({after.get(check.value)})"
        report.kept = [c.value for c in kept]
        if kept and later:
            revisions = _git(root, "rev-list", "--reverse", f"{item.sha}..HEAD", "--", rule.path).split()[:later]
            report.later_revisions = revisions
            for revision in revisions:
                outcomes = at(revision, "later", kept)
                report.later_failures += [{"revision": revision, "check": check, "outcome": outcome}
                                          for check, outcome in outcomes.items() if outcome != "pass"]
    return report


def supporting_events(project: Path, rule: rules_mod.Rule, limit: int = 5) -> list[int]:
    """IDs of existing TRACE episodic events about the governed file (read-only; empty without memory)."""
    from memory import episodic, store
    from trace_memory import project as project_mod

    database = project_mod.db_path(project)
    if not database.exists():
        return []
    conn = store.connect(database)
    try:
        return [event.id for event in episodic.search_events(conn, file=rule.path, symbol=rule.symbol, limit=limit)]
    except Exception:  # noqa: BLE001 - supporting evidence is optional
        return []
    finally:
        conn.close()


def render(rule: rules_mod.Rule, report: Report) -> str:
    """The proposed ``.trace/RULES.md`` block, carrying its validation evidence."""
    failed = {failure["revision"] for failure in report.later_failures}
    later = f" later-clean={len(report.later_revisions) - len(failed)}/{len(report.later_revisions)}"
    lines = [f"## {rule.path}" + (f" :: {rule.symbol}" if rule.symbol else ""),
             f"- {rule.text}", "  checks:"]
    lines += [f"    - pytest: {check}" for check in report.kept]
    lines.append(f"  <!-- {rule.provenance} fails-before={report.correction['parent'][:12]} "
                 f"passes-after={report.correction['sha'][:12]}{later} -->")
    return "\n".join(lines) + "\n"


def save(project: Path, report: Report) -> Path | None:
    """Keep the full validation report under .agent-memory/guardrails/ when TRACE memory exists."""
    from trace_memory import project as project_mod

    directory = project_mod.memory_dir(project)
    if not directory.is_dir():
        return None
    target = directory / "guardrails" / f"{report.correction['sha']}.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(asdict(report), indent=1), encoding="utf-8")
    return target
