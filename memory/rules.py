"""Approved engineering rules kept in ``.trace/RULES.md``.

The rules file is the only instruction tier TRACE presents. Authority comes
from Git, not from TRACE's database: a rule is **approved** when the same rule
(scope, text and checks) is in ``.trace/RULES.md`` on the approval ref, which
is the repository's default branch unless configured otherwise. Anything only
in the working tree or on an unmerged branch is **proposed**: shown as a
suggestion and never executed.

TRACE cannot see branch protection locally. Approval is therefore only as
strong as the review policy on that branch; ``trace rules list`` names the ref
and commit every approved rule came from.

Format::

    ## memory/store.py :: clamp_limit
    - Reject fractional floats by returning DEFAULT_LIMIT. Integral floats
      still clamp.
      checks:
        - py: clamp_limit(8.5) == DEFAULT_LIMIT
        - pytest: tests/memory/test_store.py::test_clamp_limit
      <!-- trace:event=42 -->

A heading without ``:: symbol`` scopes rules to the whole file. Text outside
headings is ignored. Malformed rules are reported, never silently dropped.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from memory import redact as redact_mod
from memory import synthetic
from memory import task_library

RULES_PATH = ".trace/RULES.md"
MAX_RULE_CHARS = 300
MAX_CHECKS = 6
MAX_CHECK_CHARS = 200
TITLE = f"{synthetic.SYNTHETIC_LABEL} - PROJECT RULES]"

_HEADING = re.compile(r"^##\s+(?P<path>[^\s:][^:]*?)\s*(?:::\s*(?P<symbol>[A-Za-z_][\w.]*))?\s*$")
_SYMBOL = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_PYTEST = re.compile(r"[A-Za-z0-9_./-]+\.py(?:::[A-Za-z_][\w\[\]-]*)*")
_CHECK = re.compile(r"^-\s+(?P<kind>py|pytest):\s*(?P<value>.+?)\s*$")


@dataclass(frozen=True)
class Check:
    kind: str  # "py" or "pytest"
    value: str


@dataclass(frozen=True)
class Rule:
    path: str
    symbol: str | None
    text: str
    checks: tuple[Check, ...] = ()
    provenance: str | None = None
    line: int = 0

    @property
    def key(self) -> str:
        """Identity for approval: scope, text and checks. Provenance and position do not count."""
        body = [self.path, self.symbol, self.text, [[c.kind, c.value] for c in self.checks]]
        return sha256(json.dumps(body, ensure_ascii=False).encode("utf-8")).hexdigest()

    @property
    def scope(self) -> str:
        return f"{self.path} {self.symbol}" if self.symbol else self.path


@dataclass
class RuleSet:
    approved: list[Rule] = field(default_factory=list)
    proposed: list[Rule] = field(default_factory=list)
    removed_locally: list[Rule] = field(default_factory=list)  # approved, but deleted in the working tree
    errors: list[str] = field(default_factory=list)
    ref: str | None = None
    commit: str | None = None


def _valid_py(expression: str) -> str | None:
    if len(expression) > MAX_CHECK_CHARS:
        return f"py check longer than {MAX_CHECK_CHARS} characters"
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return "py check is not a single Python expression"
    for node in ast.walk(tree):
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
        if name.startswith("__"):
            return "py check may not use dunder names"
        if isinstance(node, (ast.Lambda, ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom)):
            return "py check must be a plain expression"
    return None


def _check(kind: str, value: str) -> tuple[Check | None, str | None]:
    if kind == "py":
        problem = _valid_py(value)
        return (None, problem) if problem else (Check("py", value), None)
    if _PYTEST.fullmatch(value) is None or value.startswith("-") or not task_library.is_git_path(value.split("::")[0]):
        return None, "pytest check must be a repository-relative test node ID"
    return Check("pytest", value), None


def parse(text: str) -> tuple[list[Rule], list[str]]:
    """Parse a rules file. Returns (rules, errors); errors name the line."""
    rules: list[Rule] = []
    errors: list[str] = []
    path = symbol = None
    current: dict | None = None
    in_checks = False

    def finish():
        nonlocal current
        if current is None:
            return
        body = " ".join(current["text"]).strip()
        problems = list(current["problems"])
        if not body:
            problems.append("rule has no text")
        if len(body) > MAX_RULE_CHARS:
            problems.append(f"rule longer than {MAX_RULE_CHARS} characters")
        if redact_mod.redact_text(body) != body:
            problems.append("rule text looks like it contains a secret")
        if len(current["checks"]) > MAX_CHECKS:
            problems.append(f"more than {MAX_CHECKS} checks")
        if problems:
            errors.extend(f"line {current['line']}: {problem}" for problem in problems)
        else:
            rules.append(Rule(path, symbol, body, tuple(current["checks"]), current["provenance"], current["line"]))
        current = None

    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        stripped = line.strip()
        if line.startswith("#"):
            finish()
            heading = _HEADING.match(line)
            if line.startswith("## "):
                if heading is None or not task_library.is_git_path(heading["path"]):
                    errors.append(f"line {number}: heading must be '## <repo-relative path>' or '## <path> :: <symbol>'")
                    path = symbol = None
                else:
                    path, symbol = heading["path"], heading["symbol"]
            continue
        if line.startswith("- "):
            finish()
            if path is None:
                errors.append(f"line {number}: rule outside a valid '## path' section")
                continue
            current = {"line": number, "text": [stripped[2:]], "checks": [], "provenance": None, "problems": []}
            in_checks = False
            continue
        if current is None or not stripped:
            continue
        if not raw.startswith((" ", "\t")):
            finish()
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            current["provenance"] = stripped[4:-3].strip() or None
        elif stripped == "checks:":
            in_checks = True
        elif in_checks:
            match = _CHECK.match(stripped)
            if match is None:
                current["problems"].append(f"unrecognized check on line {number}; use '- py: <expr>' or '- pytest: <node>'")
                continue
            check, problem = _check(match["kind"], match["value"])
            if problem:
                current["problems"].append(f"{problem} (line {number})")
            else:
                current["checks"].append(check)
        else:
            current["text"].append(stripped)
    finish()
    return rules, errors


def approval_ref(root: Path, override: str | None = None) -> tuple[str, str] | None:
    """(ref, commit) whose rules file is authoritative, or None when unavailable."""
    candidates = [override or os.environ.get("TRACE_RULES_REF")]
    if not candidates[0]:
        try:
            head = task_library.local_git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").decode().strip()
            candidates = [head] if head else []
        except ValueError:
            candidates = []
        candidates += ["refs/heads/main", "refs/heads/master"]
    for ref in candidates:
        if not ref:
            continue
        try:
            commit = task_library.local_git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").decode().strip()
        except ValueError:
            continue
        if task_library.is_commit(commit):
            return ref, commit
    return None


def _at(root: Path, commit: str) -> str | None:
    try:
        return task_library.local_git(root, "show", f"{commit}:{RULES_PATH}").decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def load(root: Path, *, ref: str | None = None) -> RuleSet:
    """Approved rules from the approval ref; proposed rules from the working tree."""
    result = RuleSet()
    authority = approval_ref(root, ref)
    if authority is not None:
        result.ref, result.commit = authority
        approved_text = _at(root, result.commit)
        if approved_text is not None:
            result.approved, errors = parse(approved_text)
            result.errors += [f"{result.ref}:{RULES_PATH} {error}" for error in errors]
    else:
        result.errors.append("no approval ref found (default branch); every rule is treated as proposed")
    local_file = root / RULES_PATH
    local: list[Rule] = []
    if local_file.is_file():
        local, errors = parse(local_file.read_text(encoding="utf-8"))
        result.errors += [f"{RULES_PATH} {error}" for error in errors]
    approved_keys = {rule.key for rule in result.approved}
    local_keys = {rule.key for rule in local}
    result.proposed = [rule for rule in local if rule.key not in approved_keys]
    result.removed_locally = [rule for rule in result.approved if rule.key not in local_keys] if local_file.is_file() else []
    return result


def scoped(rules: list[Rule], paths: list[str] | None = None, symbols: list[str] | None = None) -> list[Rule]:
    """Rules that govern the given files (and, when named, definitions)."""
    if not paths:
        return list(rules)
    wanted = {path.replace("\\", "/") for path in paths}
    names = set(symbols or ())
    return [rule for rule in rules if rule.path in wanted
            and (rule.symbol is None or not names or rule.symbol in names or rule.symbol.split(".")[-1] in names)]


def _line(text: str) -> str:
    safe = " ".join(redact_mod.redact_text(text).split())
    return synthetic._INVISIBLE.sub(lambda m: json.dumps(m.group(), ensure_ascii=True)[1:-1], safe)


def render(rule_set: RuleSet, paths: list[str] | None = None, symbols: list[str] | None = None) -> str:
    """Plain-sentence rules block. Approved rules are instructions; proposed ones are labeled suggestions.

    Rules are rendered as sentences, not records: on the frozen framing probe a
    small model attempted a rule 9 in 10 times as a sentence and 3 in 10 times
    inside a JSON record, with the same text.
    """
    approved = scoped(rule_set.approved, paths, symbols)
    proposed = scoped(rule_set.proposed, paths, symbols)
    if not approved and not proposed:
        return ""
    lines = [TITLE]
    if approved:
        where = f" (approved on {rule_set.ref} at {rule_set.commit[:12]})" if rule_set.ref and rule_set.commit else ""
        lines.append(f"Project rules for the code in this task{where}. Follow them unless the user's request "
                     "says otherwise; if a rule conflicts with the request or the current code, say so.")
        for rule in approved:
            lines.append(f"- {_line(rule.scope)}: {_line(rule.text)}")
            lines += [f"  Check: {_line(check.value)}" for check in rule.checks]
    if proposed:
        lines.append("Proposed rules, not yet approved. Treat them as suggestions, not requirements:")
        lines += [f"- {_line(rule.scope)}: {_line(rule.text)}" for rule in proposed]
    return "\n".join(lines)
