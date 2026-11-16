"""Approved rules: parsing, Git-based approval, sentence rendering and check diagnosis."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from memory import rule_checks, rules, synthetic

RULES = """# Project rules
Prose outside sections is ignored.

## pkg/limits.py :: clamp_limit
- Reject fractional floats by returning DEFAULT_LIMIT.
  Integral floats still clamp.
  checks:
    - py: clamp_limit(8.5) == DEFAULT_LIMIT
    - pytest: tests/test_limits.py::test_floats
  <!-- trace:event=42 -->

## pkg/limits.py
- Keep every public function free of I/O.
"""

SOURCE = "DEFAULT_LIMIT = 10\n\ndef clamp_limit(limit):\n    if isinstance(limit, float) and not limit.is_integer():\n        return DEFAULT_LIMIT\n    return max(1, min(50, int(limit)))\n"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                           *args], cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "limits.py").write_text(SOURCE, encoding="utf-8")
    (tmp_path / ".trace").mkdir()
    (tmp_path / ".trace" / "RULES.md").write_text(RULES, encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "rules")
    return tmp_path


def test_parse_scopes_text_checks_and_provenance():
    parsed, errors = rules.parse(RULES)
    assert errors == []
    first, second = parsed
    assert (first.path, first.symbol) == ("pkg/limits.py", "clamp_limit")
    assert first.text == "Reject fractional floats by returning DEFAULT_LIMIT. Integral floats still clamp."
    assert [(c.kind, c.value) for c in first.checks] == [
        ("py", "clamp_limit(8.5) == DEFAULT_LIMIT"), ("pytest", "tests/test_limits.py::test_floats")]
    assert first.provenance == "trace:event=42"
    assert (second.symbol, second.checks) == (None, ())


@pytest.mark.parametrize("body, problem", [
    ("- orphan rule\n", "outside a valid"),
    ("## ../etc :: x\n- r\n", "heading must be"),
    ("## a.py\n- " + "x" * 301 + "\n", "longer than 300"),
    ("## a.py\n- r\n  checks:\n    - py: __import__('os').system('x')\n", "dunder"),
    ("## a.py\n- r\n  checks:\n    - py: (lambda: 1)()\n", "plain expression"),
    ("## a.py\n- r\n  checks:\n    - py: x = 1\n", "single Python expression"),
    ("## a.py\n- r\n  checks:\n    - pytest: -k evil\n", "test node ID"),
    ("## a.py\n- r\n  checks:\n    - sh: rm -rf /\n", "unrecognized check"),
    ("## a.py\n- token=ghp_" + "a" * 36 + "\n", "secret"),
])
def test_malformed_rules_are_reported_not_dropped_silently(body, problem):
    parsed, errors = rules.parse(body)
    assert parsed == [] and any(problem in error for error in errors), errors


def test_approval_comes_from_the_default_branch_not_the_working_tree(repo):
    rule_file = repo / ".trace" / "RULES.md"
    edited = RULES.replace("Keep every public function free of I/O.", "Keep public functions free of I/O and logging.")
    rule_file.write_text(edited + "\n## pkg/new.py\n- A brand new rule.\n", encoding="utf-8")
    loaded = rules.load(repo)
    assert loaded.ref == "refs/heads/main" and len(loaded.commit) == 40
    assert [r.symbol for r in loaded.approved] == ["clamp_limit", None]
    # An edited rule needs approval again; a new rule is only proposed.
    assert sorted(r.text for r in loaded.proposed) == ["A brand new rule.", "Keep public functions free of I/O and logging."]
    # Deleting the approved original locally does not withdraw its approval.
    assert [r.text for r in loaded.removed_locally] == ["Keep every public function free of I/O."]


def test_unmerged_branch_rules_are_proposed(repo):
    git(repo, "checkout", "-q", "-b", "agent-branch")
    (repo / ".trace" / "RULES.md").write_text(RULES + "\n## pkg/x.py\n- Branch-only rule.\n", encoding="utf-8")
    git(repo, "commit", "-qam", "agent adds a rule")
    loaded = rules.load(repo)
    assert loaded.ref == "refs/heads/main"
    assert [r.text for r in loaded.proposed] == ["Branch-only rule."]


def test_no_default_branch_means_nothing_is_approved(tmp_path):
    git(tmp_path, "init", "-q", "-b", "work")
    (tmp_path / ".trace").mkdir()
    (tmp_path / ".trace" / "RULES.md").write_text(RULES, encoding="utf-8")
    loaded = rules.load(tmp_path)
    assert loaded.approved == [] and len(loaded.proposed) == 2
    assert any("no approval ref" in error for error in loaded.errors)


def test_render_is_plain_sentences_skipped_by_capture_and_scoped(repo):
    (repo / ".trace" / "RULES.md").write_text(RULES + "\n## pkg/limits.py :: other\n- Proposed only.\n", encoding="utf-8")
    loaded = rules.load(repo)
    text = rules.render(loaded, ["pkg/limits.py"], ["clamp_limit"])
    assert synthetic.is_synthetic_memory_text(text)
    assert "{" not in text and "boundary" not in text
    assert "- pkg/limits.py clamp_limit: Reject fractional floats" in text
    assert "  Check: clamp_limit(8.5) == DEFAULT_LIMIT" in text
    assert "Proposed only." not in text  # scoped to clamp_limit
    assert "Proposed rules, not yet approved" in rules.render(loaded, ["pkg/limits.py"], ["other"])
    assert rules.render(loaded, ["unrelated.py"]) == ""


def _rule(check):
    return rules.Rule("pkg/limits.py", "clamp_limit", "r", (rules.Check("py", check),))


def test_py_checks_run_against_the_governed_file(repo):
    run = rule_checks.subprocess_executor(repo)
    assert run(*(lambda r: (r, r.checks[0]))(_rule("clamp_limit(8.5) == DEFAULT_LIMIT"))) == ("pass", "")
    outcome, output = run(*(lambda r: (r, r.checks[0]))(_rule("clamp_limit(8.5) == 99")))
    assert outcome == "fail" and "False" in output
    broken = rules.Rule("pkg/missing.py", None, "r", (rules.Check("py", "1 == 1"),))
    assert run(broken, broken.checks[0])[0] == "error"


def test_failures_are_diagnosed_not_assumed_stale(repo):
    rule = _rule("clamp_limit(8.5) == 99")
    calls = []

    def flaky(rule_, check):
        calls.append(1)
        return ("fail", "first") if len(calls) == 1 else ("pass", "")

    assert rule_checks.run(repo, [rule], flaky)[0].classification == "flaky"
    assert rule_checks.run(repo, [rule], lambda r, c: ("error", "ModuleNotFoundError"))[0].classification == "environment"
    failing = lambda r, c: ("fail", "False")
    assert rule_checks.run(repo, [rule], failing)[0].classification == "stale_or_regression"
    (repo / "pkg" / "limits.py").write_text(SOURCE + "\n# edit\n", encoding="utf-8")
    result = rule_checks.run(repo, [rule], failing)[0]
    assert result.classification == "regression_in_progress"
    assert rule_checks.blocking([result]) == [result]


def test_passing_checks_are_not_classified(repo):
    result = rule_checks.run(repo, [_rule("True")], lambda r, c: ("pass", ""))[0]
    assert result.outcome == "pass" and result.classification is None and rule_checks.blocking([result]) == []


def test_mcp_get_rules_renders_scoped_rules(repo, monkeypatch):
    from memory import mcp_server

    monkeypatch.setattr(mcp_server, "_ROOT_PATH", repo)
    monkeypatch.setattr(mcp_server, "_INTEGRATION_MARKER", None)
    monkeypatch.setattr(mcp_server, "_CHECK_INTEGRATION", None)
    text = mcp_server.get_rules("pkg/limits.py", "clamp_limit")
    assert "Reject fractional floats" in text and "Check: clamp_limit(8.5) == DEFAULT_LIMIT" in text
    assert mcp_server.get_rules("elsewhere.py") == "No approved or proposed rules for this scope."
    with pytest.raises(ValueError):
        mcp_server.get_rules("../outside.py")


def test_mentioned_prefers_the_named_function_then_the_named_file():
    parsed, _ = rules.parse(RULES + "\n## pkg/limits.py :: parse_limit\n- Strip whitespace first.\n"
                            "\n## other/limits.py :: clamp_limit\n- Other file's rule.\n")
    texts = lambda request: [rule.text.split(".")[0] for rule in rules.mentioned(parsed, request)]
    assert texts("Fix clamp_limit.\nFile: pkg/limits.py") == ["Reject fractional floats by returning DEFAULT_LIMIT",
                                                              "Keep every public function free of I/O"]
    assert texts("Tidy pkg/limits.py") == ["Keep every public function free of I/O"]  # file rules only
    assert texts("Fix parse_quota in pkg/limits.py") == ["Keep every public function free of I/O"]  # no rule for it
    assert texts("clamp_limit is wrong") == ["Reject fractional floats by returning DEFAULT_LIMIT", "Other file's rule"]
    assert texts("my_clamp_limit2 and clamp_limits") == []  # whole words only
    assert texts("unrelated request") == []


def test_render_notes_is_the_bare_sentence():
    parsed, _ = rules.parse(RULES)
    assert rules.render_notes(parsed[:1]) == "Reject fractional floats by returning DEFAULT_LIMIT. Integral floats still clamp."
    assert rules.render_notes(parsed) == ("- Reject fractional floats by returning DEFAULT_LIMIT. Integral floats still clamp.\n"
                                          "- Keep every public function free of I/O.")
    assert rules.render_notes([]) == ""
    hidden = rules.Rule("a.py", None, "Use​ ascii")
    assert "​" not in rules.render_notes([hidden])


def test_local_branch_approval_is_flagged(repo):
    assert any("local branch refs/heads/main" in error for error in rules.load(repo).errors)


def test_next_step_follows_the_diagnosis(repo):
    rule = rules.Rule("pkg/limits.py", "clamp_limit", "r", (rules.Check("py", "False"),), provenance="trace:event=42")
    result = rule_checks.CheckResult(rule, rule.checks[0], "fail", "False", "regression_in_progress", "d")
    step = rule_checks.next_step(result, repeated=True, changes=["abc123 edit"])
    assert "Fix the code" in step and "already failed" in step and "abc123 edit" in step and "trace:event=42" in step
    assert "do not change code" in rule_checks.next_step(rule_checks.CheckResult(rule, rule.checks[0], "error", "", "environment"))
    assert rule_checks.next_step(rule_checks.CheckResult(rule, rule.checks[0], "pass", "")) == ""


def test_changes_since_lists_commits_not_on_the_approval_ref(repo):
    git(repo, "checkout", "-q", "-b", "work")
    (repo / "pkg" / "limits.py").write_text(SOURCE + "\n# edit\n", encoding="utf-8")
    git(repo, "commit", "-qam", "loosen limits")
    assert [line.split(" ", 1)[1] for line in rule_checks.changes_since(repo, "refs/heads/main", "pkg/limits.py")] == ["loosen limits"]
    assert rule_checks.changes_since(repo, "refs/heads/main", "pkg/other.py") == []
    assert rule_checks.changes_since(repo, None, "pkg/limits.py") == []


def test_results_carry_the_file_digest(repo):
    result = rule_checks.run(repo, [_rule("True")], lambda r, c: ("pass", ""))[0]
    import hashlib
    assert result.digest == hashlib.sha256((repo / "pkg" / "limits.py").read_bytes()).hexdigest()


def test_py_checks_import_package_modules_with_relative_imports(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "base.py").write_text("DEFAULT = 10\n", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "limits.py").write_text("from .base import DEFAULT\n\ndef clamp(x):\n    return x if x > 0 else DEFAULT\n",
                                                        encoding="utf-8")
    rule = rules.Rule("src/pkg/limits.py", "clamp", "r", (rules.Check("py", "clamp(-1) == DEFAULT"),))
    assert rule_checks.subprocess_executor(tmp_path)(rule, rule.checks[0]) == ("pass", "")


def test_checks_resolving_outside_the_repository_are_not_run(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.py").write_text("X = 1\n", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)], check=True, capture_output=True)
    rule = rules.Rule("link/target.py", None, "r", (rules.Check("py", "X == 1"),))
    outcome, output = rule_checks.subprocess_executor(root)(rule, rule.checks[0])
    assert outcome == "error" and "outside the repository" in output


def test_pythonpath_puts_the_tree_first(tmp_path):
    (tmp_path / "src" / "tracefixturelib").mkdir(parents=True)
    (tmp_path / "src" / "tracefixturelib" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "probe.py").write_text("import tracefixturelib\n", encoding="utf-8")
    rule = rules.Rule("probe.py", None, "r", (rules.Check("py", "tracefixturelib.VALUE == 1"),))
    assert rule_checks.subprocess_executor(tmp_path)(rule, rule.checks[0])[0] == "error"  # src not on the path
    assert rule_checks.subprocess_executor(tmp_path, pythonpath=("src",))(rule, rule.checks[0]) == ("pass", "")
