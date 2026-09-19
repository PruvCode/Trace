"""End-to-end runner tests with the mock agent (no LLM, no network)."""

from pathlib import Path

from benchmark import loader as loader_mod
from benchmark import results as results_mod
from benchmark import runner as runner_mod


def test_discover_tasks_skips_template(repo_root):
    """Corpus discovery must yield runnable tasks only, never scaffolding."""
    found = runner_mod.discover_tasks(repo_root / "tasks")
    rel = sorted(str(p.parent.relative_to(repo_root / "tasks")) for p in found)
    assert rel == [
        str(Path("A_control") / "task_01_timeout_fix"),
        str(Path("A_control") / "task_02_empty_user"),
        str(Path("A_control") / "task_03_token_prefix"),
        str(Path("A_control") / "task_04_page_offbyone"),
        str(Path("A_control") / "task_05_retry_count"),
        str(Path("A_control") / "task_06_csv_header"),
        str(Path("B_structural") / "task_01_callers"),
        str(Path("B_structural") / "task_02_definition"),
        str(Path("B_structural") / "task_03_arg_order"),
        str(Path("B_structural") / "task_04_serde_contract"),
        str(Path("B_structural") / "task_05_exception_contract"),
        str(Path("B_structural") / "task_06_shadowed_name"),
        str(Path("C_episodic") / "task_01_failed_attempt"),
        str(Path("C_episodic") / "task_02_decision"),
        str(Path("C_episodic") / "task_03_incident_blocklist"),
        str(Path("C_episodic") / "task_04_reverted_cache_key"),
        str(Path("C_episodic") / "task_05_cursor_rollout"),
        str(Path("C_episodic") / "task_06_migration_idempotent"),
        str(Path("D_staleness") / "task_01_suffix"),
        str(Path("D_staleness") / "task_02_credential_rotation"),
        str(Path("D_staleness") / "task_03_stale_page_default"),
        str(Path("D_staleness") / "task_04_stale_route"),
    ]


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
