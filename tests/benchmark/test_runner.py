"""End-to-end runner tests with the mock agent (no LLM, no network)."""

from benchmark import loader as loader_mod
from benchmark import results as results_mod
from benchmark import runner as runner_mod


def _load(repo_root):
    task = loader_mod.load_task(
        repo_root / "tasks" / "A_control" / "task_01_timeout_fix", repo_root
    )
    config = loader_mod.load_config(repo_root / "configs" / "baseline.yaml")
    return task, config


def test_e2e_pass(tmp_path, repo_root):
    task, config = _load(repo_root)
    results_path = tmp_path / "results.jsonl"
    outcome = runner_mod.run_single(
        task, config, 1, 0, tmp_path / "ws", "exp_test",
        results_path, repo_root, mock_behavior_override="pass",
    )
    assert outcome["success"] is True
    assert outcome["turns"] == 3
    assert outcome["tool_calls"] == 3
    assert outcome["input_tokens"] is None
    assert outcome["base_commit"] == task.base_commit
    assert outcome["seed"] == "0-1"
    rows = results_mod.read_results(results_path)
    assert len(rows) == 1
    results_mod.validate_result(rows[0])
    assert rows[0]["task_version"] == task.task_version
    assert rows[0]["config_hash"] == config["config_hash"]


def test_e2e_fail(tmp_path, repo_root):
    """Proves the evaluator (not the agent's claim) decides the outcome."""
    task, config = _load(repo_root)
    outcome = runner_mod.run_single(
        task, config, 1, 0, tmp_path / "ws", "exp_test",
        tmp_path / "results.jsonl", repo_root, mock_behavior_override="fail",
    )
    assert outcome["success"] is False
    assert outcome["termination_reason"] == "done_tool_called"


def test_multi_run_same_base(tmp_path, repo_root):
    task, config = _load(repo_root)
    outcomes = runner_mod.run_experiment(
        [repo_root / "tasks" / "A_control" / "task_01_timeout_fix" / "task.yaml"],
        config, 2, 7, tmp_path / "ws", "exp_multi",
        tmp_path / "results.jsonl", repo_root, "pass",
    )
    assert len(outcomes) == 2
    assert all(o["success"] for o in outcomes)
    assert [o["seed"] for o in outcomes] == ["7-1", "7-2"]
    assert outcomes[0]["workspace"] != outcomes[1]["workspace"]
    rows = results_mod.read_results(tmp_path / "results.jsonl")
    assert len(rows) == 2
    for row in rows:
        results_mod.validate_result(row)
        assert row["base_commit"] == task.base_commit


def test_baseline_has_no_memory_tools(repo_root, tmp_path):
    """Proves the baseline exposes only core file tools (no memory seam)."""
    from agent.tools import build_core_tools

    config = loader_mod.load_config(repo_root / "configs" / "baseline.yaml")
    assert config["backend"] is None
    assert config.get("prompt_addendum", "") == ""
    names = {t.name for t in build_core_tools(tmp_path)}
    assert names == {"read_file", "write_file", "done"}
