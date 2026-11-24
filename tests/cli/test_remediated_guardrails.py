"""Verification tests exercise actual runner outcomes and approval boundaries."""

import json
import subprocess

import pytest

from memory import rule_checks
from trace_memory import cli


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=missing-hooks", *args],
        cwd=root, text=True, encoding="utf-8",
    ).strip()


def save(root, approved):
    (root / ".trace").mkdir(exist_ok=True)
    (root / ".trace/RULES.md").write_text(approved, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "review rules")
    return git(root, "rev-parse", "HEAD")


def check(root, capsys, *args):
    result = cli.main(["rules", "check", str(root), "--all", "--json", *args])
    return result, json.loads(capsys.readouterr().out)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("COUNT = 1\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(("program", "node", "classification", "skipped"), [
    ("import pytest\n@pytest.mark.skip(reason='temporarily unavailable')\ndef test_rule(): pass\n",
     "::test_rule", "environment", 1),
    ("import pytest\n@pytest.mark.xfail(reason='known failure')\ndef test_rule(): assert False\n",
     "::test_rule", "environment", 1),
    ("import pytest\n@pytest.mark.xfail(reason='expected failure')\ndef test_rule(): assert True\n",
     "::test_rule", "environment", 1),
    ("VALUE = 1\n", "", "environment", 0),
    ("def test_present(): pass\n", "::test_absent", "missing_check", 0),
    ("import a_trace_fixture_module_that_does_not_exist\ndef test_rule(): pass\n",
     "::test_rule", "environment", 0),
])
def test_real_pytest_unverified_states(repo, capsys, program, node, classification, skipped):
    (repo / "test_app.py").write_text(program, encoding="utf-8")
    save(repo, f"## app.py\n- Check application.\n  checks:\n    - pytest: test_app.py{node}\n")
    code, report = check(repo, capsys)
    assert code == 3 and report["verdict"] == "inconclusive"
    assert report["checked"] == 0 and report["skipped"] == skipped
    assert report["results"][0]["classification"] == classification


def test_ordinary_pytest_pass_and_mixed_failure_override(repo, capsys):
    (repo / "test_app.py").write_text("def test_rule(): assert True\n", encoding="utf-8")
    spec = "## app.py\n- Check app.\n  checks:\n    - pytest: test_app.py\n"
    save(repo, spec)
    code, report = check(repo, capsys)
    assert code == 0 and report["verdict"] == "verified" and report["checked"] == 1
    (repo / "test_app.py").write_text(
        "import pytest\n@pytest.mark.skip(reason='unavailable')\ndef test_skip(): pass\n"
        "def test_fail(): assert False\n", encoding="utf-8",
    )
    save(repo, spec + "- Missing import.\n  checks:\n    - py: import_check()\n")
    (repo / "app.py").write_text("import a_trace_fixture_module_that_does_not_exist\n", encoding="utf-8")
    code, report = check(repo, capsys, "--allow-inconclusive")
    assert code == 1 and report["verdict"] == "failing"
    assert report["checked"] == 1 and report["skipped"] >= 1
    assert report["override_requested"] and not report["override"] and report["inconclusive"] == 1


def test_malformed_authority_and_detached_gate(repo, tmp_path, capsys):
    committed = save(repo, "## app.py\n- Valid.\n  checks:\n    - py: COUNT == 1\n"
                          "- Broken.\n  checks:\n    - py: COUNT ==\n")
    code, report = check(repo, capsys, "--allow-inconclusive")
    assert code == 3 and report["verdict"] == "inconclusive" and report["checked"] == 1
    assert report["errors"] and not report["override"]
    detached = tmp_path / "other"
    detached.mkdir()
    git(detached, "init", "-q", "-b", "pr-checkout")
    git(detached, "fetch", "--depth=1", str(repo), "main")
    git(detached, "checkout", "--detach", "FETCH_HEAD")
    code, report = check(detached, capsys, "--allow-inconclusive")
    assert code == 3 and report["checked"] == 0 and not report["override"]
    code, report = check(detached, capsys, "--ref", "refs/heads/unavailable")
    assert code == 3 and report["commit"] is None
    code, report = check(detached, capsys, "--strict", "--ref", committed)
    assert code == 3 and report["checked"] == 1


def test_strict_requires_approved_rules_and_advisory_is_empty(repo, capsys):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first commit")
    missing = git(repo, "rev-parse", "HEAD")
    assert check(repo, capsys, "--strict", "--ref", missing)[0] == 3
    approved = save(repo, "## app.py\n- Advisory rule without a check.\n")
    (repo / ".trace/RULES.md").write_text(
        "## app.py\n- Unapproved candidate.\n  checks:\n    - py: False\n", encoding="utf-8",
    )
    code, report = check(repo, capsys, "--strict", "--ref", approved)
    assert code == 0 and report["verdict"] == "nothing to check"
    assert report["uncovered"] == ["app.py"] and report["checked"] == 0


def test_runner_timeout_and_missing_interpreter(repo, capsys, monkeypatch):
    save(repo, "## app.py\n- Check app.\n  checks:\n    - py: COUNT == 1\n")
    code, report = check(repo, capsys, "--python", str(repo / "missing-python"))
    assert code == 3 and report["execution_errors"] > 0
    monkeypatch.setattr(rule_checks, "TIMEOUT", 0.001)
    code, report = check(repo, capsys)
    assert code == 3 and "timed out" in report["results"][0]["output"]


def test_actual_failure_beats_infrastructure_error_on_retry(repo):
    rule = rule_checks.Rule("app.py", None, "Fixture", (rule_checks.Check("py", "False"),))
    tries = iter([("fail", "assertion failed"), ("error", "runner closed")])
    result = rule_checks.run(repo, [rule], lambda _rule, _check: next(tries))
    assert rule_checks.verdict(result) == "failing"
