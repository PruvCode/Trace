"""`trace rules list|check` against a throwaway Git repository."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from trace_memory import cli

RULES = """## pkg/limits.py :: clamp_limit
- Reject fractional floats by returning DEFAULT_LIMIT.
  checks:
    - py: clamp_limit(8.5) == DEFAULT_LIMIT

## pkg/limits.py
- Keep every public function free of I/O.
"""
SOURCE = (
    "DEFAULT_LIMIT = 10\n\n"
    "def clamp_limit(limit):\n"
    "    if isinstance(limit, float) and not limit.is_integer():\n"
    "        return DEFAULT_LIMIT\n"
    "    return max(1, min(50, int(limit)))\n"
)


def git(root, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                           *args], cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "pkg").mkdir()
    (root / "pkg" / "limits.py").write_text(SOURCE, encoding="utf-8")
    (root / ".trace").mkdir()
    (root / ".trace" / "RULES.md").write_text(RULES, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "rules")
    return root


def test_list_separates_approved_from_proposed(repo, capsys):
    (repo / ".trace" / "RULES.md").write_text(RULES + "\n## pkg/x.py\n- Proposed rule.\n", encoding="utf-8")
    assert cli.main(["rules", "list", str(repo), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ref"] == "refs/heads/main"
    assert [(r["state"], r["scope"]) for r in out["rules"]] == [
        ("approved", "pkg/limits.py clamp_limit"), ("approved", "pkg/limits.py"), ("proposed", "pkg/x.py")]


def test_check_skips_unchanged_files_and_passes_compliant_code(repo, capsys):
    assert cli.main(["rules", "check", str(repo)]) == 0  # nothing changed since HEAD: nothing to check
    assert "0 checks" in capsys.readouterr().out
    assert cli.main(["rules", "check", str(repo), "--all"]) == 0
    assert "1 checks from 1 approved rules: 1 passed" in capsys.readouterr().out


def test_check_blocks_a_change_that_breaks_an_approved_rule(repo, capsys):
    broken = SOURCE.replace("        return DEFAULT_LIMIT\n", "        pass\n")
    (repo / "pkg" / "limits.py").write_text(broken, encoding="utf-8")
    assert cli.main(["rules", "check", str(repo), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["blocking"] == 1
    assert out["results"][0]["classification"] == "regression_in_progress"


def test_proposed_checks_are_never_executed(repo, tmp_path, capsys):
    marker = tmp_path / "ran"
    proposal = f"\n## pkg/limits.py\n- Proposed.\n  checks:\n    - py: open({str(marker)!r}, 'w') is None\n"
    (repo / ".trace" / "RULES.md").write_text(RULES + proposal, encoding="utf-8")
    (repo / "pkg" / "limits.py").write_text(SOURCE + "\n", encoding="utf-8")
    cli.main(["rules", "check", str(repo), "--file", "pkg/limits.py"])
    assert not marker.exists()
    assert "not run (proposed, not approved): pkg/limits.py" in capsys.readouterr().out


def test_cli_entry_point_is_wired():
    done = subprocess.run([sys.executable, "-m", "trace_memory.cli", "rules", "--help"], capture_output=True, text=True)
    assert done.returncode == 0 and "check" in done.stdout
