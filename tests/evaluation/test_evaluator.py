"""Evaluator tests: mechanical PASS/FAIL/timeout on real workspace trees."""

import sys

from benchmark import evaluator as evaluator_mod
from benchmark import workspace as workspace_mod


def _workspace(tmp_path, seeded_fixture, apply_fix: bool):
    fixture_dir, head = seeded_fixture
    ws = workspace_mod.create_workspace(fixture_dir, head, tmp_path / "ws")
    if apply_fix:
        session = ws / "auth" / "session.py"
        session.write_text(
            session.read_text(encoding="utf-8").replace(
                "return 15", "return SESSION_TIMEOUT_MINUTES"
            ),
            encoding="utf-8",
        )
    return ws


def test_fail_on_buggy_tree(tmp_path, seeded_fixture):
    ws = _workspace(tmp_path, seeded_fixture, apply_fix=False)
    result = evaluator_mod.run_evaluator(
        ws, ["python", "-m", "pytest", "tests/test_session.py", "-q"], 120
    )
    assert result.success is False
    assert result.exit_code != 0
    assert not result.timed_out
    assert "assert" in result.stdout.lower() or "failed" in result.stdout.lower()


def test_pass_on_fixed_tree(tmp_path, seeded_fixture):
    ws = _workspace(tmp_path, seeded_fixture, apply_fix=True)
    result = evaluator_mod.run_evaluator(
        ws, ["python", "-m", "pytest", "tests/test_session.py", "-q"], 120
    )
    assert result.success is True
    assert result.exit_code == 0
    assert not result.timed_out


def test_evaluator_timeout(tmp_path):
    result = evaluator_mod.run_evaluator(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(10)"],
        1,
    )
    assert result.success is False
    assert result.timed_out is True
    assert result.exit_code is None
