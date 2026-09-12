"""Robustness: crash/timeout paths still produce schema-valid results."""

import copy

from benchmark import loader as loader_mod
from benchmark import results as results_mod
from benchmark import runner as runner_mod


def test_error_behavior_writes_result(tmp_path, repo_root):
    task, config = (
        loader_mod.load_task(
            repo_root / "tasks" / "A_control" / "task_01_timeout_fix", repo_root
        ),
        loader_mod.load_config(repo_root / "configs" / "baseline.yaml"),
    )
    results_path = tmp_path / "results.jsonl"
    outcome = runner_mod.run_single(
        task, config, 1, 0, tmp_path / "ws", "exp_err",
        results_path, repo_root, mock_behavior_override="error",
    )
    assert outcome["success"] is False
    assert outcome["error"] and "mock agent crash" in outcome["error"]
    rows = results_mod.read_results(results_path)
    assert len(rows) == 1
    results_mod.validate_result(rows[0])


def test_timeout_behavior_writes_result(tmp_path, repo_root):
    task, base_config = (
        loader_mod.load_task(
            repo_root / "tasks" / "A_control" / "task_01_timeout_fix", repo_root
        ),
        loader_mod.load_config(repo_root / "configs" / "baseline.yaml"),
    )
    config = copy.deepcopy(base_config)
    config["budget"] = dict(base_config["budget"], timeout_seconds=2)
    config["mock"] = {"behavior": "slow", "sleep_seconds": 15}
    results_path = tmp_path / "results.jsonl"
    outcome = runner_mod.run_single(
        task, config, 1, 0, tmp_path / "ws", "exp_timeout",
        results_path, repo_root,
    )
    assert outcome["success"] is False
    assert outcome["termination_reason"].startswith("agent exceeded")
    rows = results_mod.read_results(results_path)
    assert len(rows) == 1
    results_mod.validate_result(rows[0])
