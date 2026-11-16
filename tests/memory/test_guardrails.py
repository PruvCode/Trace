"""Correction-to-rule prototype on a throwaway repository with a real fix history."""

from __future__ import annotations

import subprocess

import pytest

from memory import guardrails, rules

BUGGY = "def clamp(x):\n    return min(50, x)\n"
FIXED = "def clamp(x):\n    if x < 1:\n        return 1\n    return min(50, x)\n"
LATER = "def clamp(x):\n    # tidy\n    if x < 1:\n        return 1\n    return min(50, x)\n"
BASE_TEST = "from lib import clamp\n\ndef test_upper():\n    assert clamp(99) == 50\n"
FIX_TEST = BASE_TEST + "\ndef test_lower():\n    assert clamp(-5) == 1\n\ndef test_always_true():\n    assert clamp(3) == 3\n"


def git(root, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                           *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def commit(root, files, message):
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    commit(root, {"lib.py": BUGGY, "tests/test_lib.py": BASE_TEST}, "start")
    fix = commit(root, {"lib.py": FIXED, "tests/test_lib.py": FIX_TEST}, "fix: clamp values below 1 to 1 (#12)")
    return root, fix


def test_correction_and_draft_come_from_the_fix_commit(history):
    root, fix = history
    item = guardrails.correction(root, fix)
    assert (item.source_files, item.test_files) == (["lib.py"], ["tests/test_lib.py"])
    rule = guardrails.draft(root, item)
    assert (rule.path, rule.symbol, rule.text) == ("lib.py", "clamp", "Clamp values below 1 to 1.")
    assert [c.value for c in rule.checks] == ["tests/test_lib.py::test_lower", "tests/test_lib.py::test_always_true"]


def test_only_checks_that_fail_before_and_pass_after_survive(history, tmp_path):
    root, fix = history
    commit(root, {"lib.py": LATER}, "tidy")
    item = guardrails.correction(root, fix)
    report = guardrails.validate(root, item, guardrails.draft(root, item), later=5, scratch=tmp_path)
    assert report.kept == ["tests/test_lib.py::test_lower"]
    assert "does not fail before" in report.dropped["tests/test_lib.py::test_always_true"]
    assert len(report.later_revisions) == 1 and report.later_failures == []
    roles = {(run["role"], run["check"]): run["outcome"] for run in report.runs}
    assert roles["before", "tests/test_lib.py::test_lower"] == "fail"
    assert roles["after", "tests/test_lib.py::test_lower"] == "pass"
    assert subprocess.run(["git", "worktree", "list"], cwd=root, capture_output=True, text=True).stdout.count("\n") == 1


def test_a_later_change_that_breaks_the_guardrail_is_reported(history, tmp_path):
    root, fix = history
    commit(root, {"lib.py": BUGGY}, "regress")
    item = guardrails.correction(root, fix)
    report = guardrails.validate(root, item, guardrails.draft(root, item), later=5, scratch=tmp_path)
    assert report.kept == ["tests/test_lib.py::test_lower"]
    assert [failure["outcome"] for failure in report.later_failures] == ["fail"]


def test_proposal_carries_its_evidence_and_parses_as_a_proposed_rule(history, tmp_path):
    root, fix = history
    item = guardrails.correction(root, fix)
    rule = guardrails.draft(root, item)
    block = guardrails.render(rule, guardrails.validate(root, item, rule, later=0, scratch=tmp_path))
    parsed, errors = rules.parse(block)
    assert errors == [] and [c.value for c in parsed[0].checks] == ["tests/test_lib.py::test_lower"]
    assert f"fails-before={item.parent[:12]}" in parsed[0].provenance and f"correction={fix[:12]}" in parsed[0].provenance


def test_commits_without_both_code_and_tests_are_not_corrections(history):
    root, _ = history
    docs = commit(root, {"README.md": "x\n", "lib.py": FIXED + "\n"}, "docs")
    with pytest.raises(ValueError, match="both non-test code and tests"):
        guardrails.correction(root, docs)


def test_cli_proposes_writes_a_proposed_rule_and_keeps_the_report(history, capsys):
    from trace_memory import cli

    root, fix = history
    (root / ".agent-memory").mkdir()
    assert cli.main(["rules", "propose", fix, str(root), "--later", "0", "--write"]) == 0
    out = capsys.readouterr()
    assert "dropped tests/test_lib.py::test_always_true" in out.out and "proposed rule" in out.out
    text = (root / ".trace" / "RULES.md").read_text(encoding="utf-8")
    parsed, errors = rules.parse(text)
    assert errors == [] and parsed[0].symbol == "clamp"
    loaded = rules.load(root)
    assert loaded.approved == [] and [r.scope for r in loaded.proposed] == ["lib.py clamp"]  # never approved by TRACE
    assert (root / ".agent-memory" / "guardrails" / f"{fix}.json").exists()


def test_cli_refuses_a_commit_with_no_discriminating_check(history, capsys):
    from trace_memory import cli

    root, _ = history
    noop = commit(root, {"lib.py": FIXED + "\n", "tests/test_lib.py": FIX_TEST + "\ndef test_more():\n    assert clamp(4) == 4\n"},
                  "add a test")
    assert cli.main(["rules", "propose", noop, str(root), "--later", "0"]) != 0
    assert "nothing to propose" in capsys.readouterr().out


def test_later_checks_skip_commits_that_do_not_contain_the_fix(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    commit(root, {"lib.py": BUGGY, "tests/test_lib.py": BASE_TEST}, "start")
    git(root, "checkout", "-q", "-b", "side")
    commit(root, {"lib.py": BUGGY + "\n# side branch, forked before the fix\n"}, "side work")
    git(root, "checkout", "-q", "main")
    fix = commit(root, {"lib.py": FIXED, "tests/test_lib.py": FIX_TEST}, "fix: clamp values below 1 to 1")
    git(root, "merge", "-q", "--no-edit", "-X", "ours", "side")
    item = guardrails.correction(root, fix)
    report = guardrails.validate(root, item, guardrails.draft(root, item), later=5, scratch=tmp_path)
    side = git(root, "rev-parse", "side")
    assert side not in report.later_revisions  # it never contained the fix
    assert report.later_failures == []
