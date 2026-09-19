"""Repeated-run validation with fake clients: no network, no keys, no models."""

from pathlib import Path

from agent.llm_agent import LLMAgent, LLMClient, LLMResponse, LLMToolCall, LLMUsage
from benchmark import loader as loader_mod
from benchmark import results as results_mod
from benchmark import runner as runner_mod


class _DoneFake(LLMClient):
    """Completes immediately with exact usage. Deterministic."""

    def __init__(self, in_tok=100, out_tok=50):
        self.calls = 0
        self.in_tok = in_tok
        self.out_tok = out_tok

    def complete(self, messages, tools):
        self.calls += 1
        return LLMResponse(
            tool_calls=[LLMToolCall(id="1", name="done", args={"summary": "s"})],
            usage=LLMUsage(self.in_tok, self.out_tok),
        )


def _load(repo_root):
    task = loader_mod.load_task(
        repo_root / "tasks" / "A_control" / "task_01_timeout_fix", repo_root
    )
    baseline = loader_mod.load_config(repo_root / "configs" / "baseline.yaml")
    reference = loader_mod.load_config(repo_root / "configs" / "reference_memory.yaml")
    return task, baseline, reference


def test_repeated_baseline_runs_fresh_workspaces(tmp_path, repo_root, monkeypatch):
    task, baseline, _reference = _load(repo_root)
    monkeypatch.setattr(
        runner_mod, "_create_agent", lambda *a, **k: LLMAgent(_DoneFake(), model="fake")
    )
    results_path = tmp_path / "results.jsonl"
    outcomes = runner_mod.run_experiment(
        [repo_root / "tasks" / "A_control" / "task_01_timeout_fix"],
        baseline, 3, 7, tmp_path / "ws", "exp_rep",
        results_path, repo_root,
    )
    assert len(outcomes) == 3
    assert [o["run"] for o in outcomes] == [1, 2, 3]
    assert [o["seed"] for o in outcomes] == ["7-1", "7-2", "7-3"]
    workspaces = [o["workspace"] for o in outcomes]
    assert len(set(workspaces)) == 3
    for outcome in outcomes:
        assert outcome["input_tokens"] == 100
        assert outcome["total_tokens"] == 150
        assert outcome["token_source"] == "provider"
        assert outcome["timestamp"]
        assert outcome["benchmark_version"] == "0.1.0"
    rows = results_mod.read_results(results_path)
    assert len(rows) == 3
    for row in rows:
        results_mod.validate_result(row)


def test_repeated_reference_runs_fresh_dbs(tmp_path, repo_root, monkeypatch):
    task, _baseline, reference = _load(repo_root)
    monkeypatch.setattr(
        runner_mod, "_create_agent", lambda *a, **k: LLMAgent(_DoneFake(), model="fake")
    )
    results_path = tmp_path / "results.jsonl"
    outcomes = runner_mod.run_experiment(
        [repo_root / "tasks" / "A_control" / "task_01_timeout_fix"],
        reference, 2, 0, tmp_path / "ws", "exp_refrep",
        results_path, repo_root,
    )
    assert len(outcomes) == 2
    dbs = [
        Path(o["workspace"]) / ".agent-memory" / "memory.db"
        for o in outcomes
    ]
    assert dbs[0] != dbs[1]
    for db in dbs:
        assert db.exists()
    # Same task content reindexed deterministically in each fresh DB.
    from memory import store as store_mod

    counts = []
    for db in dbs:
        conn = store_mod.connect(db)
        try:
            counts.append(conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0])
        finally:
            conn.close()
    assert counts[0] == counts[1] > 0


def test_execution_order_helper():
    refs = ["a", "b"]
    default = runner_mod._execution_order(refs, 2, None)
    assert default == [("a", 1), ("a", 2), ("b", 1), ("b", 2)]
    first = runner_mod._execution_order(refs, 2, 42)
    second = runner_mod._execution_order(refs, 2, 42)
    assert first == second  # deterministic per seed
    assert sorted(first) == sorted(default)  # same multiset, order may differ


def test_cli_order_flag_parses(tmp_path, repo_root):
    task, baseline, _reference = _load(repo_root)
    assert task is not None and baseline is not None
    parser = runner_mod.build_parser()
    args = parser.parse_args(
        ["--task", "tasks/A_control/task_01_timeout_fix",
         "--config", "configs/baseline.yaml", "--order", "5"]
    )
    assert args.order == 5
    args_default = parser.parse_args(
        ["--task", "tasks/A_control/task_01_timeout_fix",
         "--config", "configs/baseline.yaml"]
    )
    assert args_default.order is None
