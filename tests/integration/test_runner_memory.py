"""End-to-end runner integration: baseline vs reference (Phase 4.4).

Deterministic throughout: mock agents only, no network, no keys. Real-LLM
paths are covered by unit-level fakes; the runner taxonomy mapping for LLM
errors is proven here by injecting exploding clients through the real
run_single path.
"""

import copy

from agent.llm_agent import LLMAgent, LLMAuthError, LLMProviderError
from benchmark import loader as loader_mod
from benchmark import results as results_mod
from benchmark import runner as runner_mod
from memory import episodic as episodic_mod
from memory import store as store_mod
from memory.reference import workspace_db_path


def _load(repo_root):
    task = loader_mod.load_task(
        repo_root / "tasks" / "A_control" / "task_01_timeout_fix", repo_root
    )
    baseline = loader_mod.load_config(repo_root / "configs" / "baseline.yaml")
    reference = loader_mod.load_config(repo_root / "configs" / "reference_memory.yaml")
    return task, baseline, reference


def test_e2e_baseline(tmp_path, repo_root):
    task, baseline, _reference = _load(repo_root)
    results_path = tmp_path / "results.jsonl"
    outcome = runner_mod.run_single(
        task, baseline, 1, 0, tmp_path / "ws", "exp_base",
        results_path, repo_root, mock_behavior_override="pass",
    )
    assert outcome["success"] is True
    assert outcome["memory_tool_calls"] == 0
    assert outcome["token_source"] == "unknown"
    assert outcome["input_tokens"] is None
    assert outcome["total_tokens"] is None
    results_mod.validate_result(results_mod.read_results(results_path)[0])


def test_e2e_reference_memory(tmp_path, repo_root):
    task, _baseline, reference = _load(repo_root)
    results_path = tmp_path / "results.jsonl"
    outcome = runner_mod.run_single(
        task, reference, 1, 0, tmp_path / "ws", "exp_ref",
        results_path, repo_root,
    )
    assert outcome["success"] is True
    assert outcome["configuration"] == "reference_memory"
    assert outcome["memory_tool_calls"] == 2
    assert outcome["tool_calls"] == 5
    assert outcome["token_source"] == "unknown"
    from pathlib import Path

    db = workspace_db_path(Path(outcome["workspace"]))
    assert db.exists()
    conn = store_mod.connect(db)
    try:
        rows = episodic_mod.search_events(conn)
        assert len(rows) == 1 and rows[0].symbol == "get_session_timeout"
        assert store_mod.find_definition(conn, "refresh_token") != []
    finally:
        conn.close()


def test_reference_runs_isolated(tmp_path, repo_root):
    task, _baseline, reference = _load(repo_root)
    results_path = tmp_path / "results.jsonl"
    first = runner_mod.run_single(
        task, reference, 1, 0, tmp_path / "ws", "exp_iso",
        results_path, repo_root,
    )
    second = runner_mod.run_single(
        task, reference, 2, 0, tmp_path / "ws", "exp_iso",
        results_path, repo_root,
    )
    assert first["workspace"] != second["workspace"]
    from pathlib import Path

    for outcome in (first, second):
        assert outcome["success"] is True
        conn = store_mod.connect(workspace_db_path(Path(outcome["workspace"])))
        try:
            # Exactly one event per run: no cross-run contamination.
            assert len(episodic_mod.search_events(conn)) == 1
        finally:
            conn.close()


def test_unknown_backend_fails_loudly(tmp_path, repo_root):
    task, baseline, _reference = _load(repo_root)
    config = copy.deepcopy(baseline)
    config["name"] = "bogus"
    config["backend"] = "bogus"
    outcome = runner_mod.run_single(
        task, config, 1, 0, tmp_path / "ws", "exp_bad",
        tmp_path / "results.jsonl", repo_root,
    )
    assert outcome["success"] is False
    assert outcome["error"] == "unknown_backend: 'bogus'"


def test_memory_setup_failure_is_not_baseline(tmp_path, repo_root):
    task, _baseline, reference = _load(repo_root)
    config = copy.deepcopy(reference)
    config["memory"] = {"preseed": [{"type": "bogus", "repo": "r"}]}
    outcome = runner_mod.run_single(
        task, config, 1, 0, tmp_path / "ws", "exp_setupfail",
        tmp_path / "results.jsonl", repo_root,
    )
    assert outcome["success"] is False
    assert outcome["configuration"] == "reference_memory"
    assert outcome["error"].startswith("memory_setup_failed:")
    assert outcome["memory_tool_calls"] == 0


def test_llm_taxonomy_through_runner(tmp_path, repo_root, monkeypatch):
    task, baseline, _reference = _load(repo_root)

    class ExplodingAuth:
        def __call__(self, *args, **kwargs):
            raise LLMAuthError("bad key")

    class ExplodingProvider:
        def __call__(self, *args, **kwargs):
            raise LLMProviderError("boom")

    for index, (factory, prefix) in enumerate(
        (
            (ExplodingAuth, "llm_auth_missing:"),
            (ExplodingProvider, "llm_provider_error:"),
        )
    ):
        exploding = factory()

        def _agent(_config, _override=None, _exploding=exploding):
            raise _exploding()

        monkeypatch.setattr(runner_mod, "_create_agent", _agent)
        outcome = runner_mod.run_single(
            task, baseline, index + 1, 0, tmp_path / "ws", "exp_llmerr",
            tmp_path / "results.jsonl", repo_root,
        )
        assert outcome["success"] is False
        assert outcome["error"].startswith(prefix), outcome["error"]


def test_llm_agent_result_tokens_reach_jsonl(tmp_path, repo_root, monkeypatch):
    """Proves AgentResult tokens flow into the result line (fake, exact)."""
    from agent.llm_agent import LLMResponse, LLMToolCall, LLMUsage
    from agent.llm_agent import LLMClient

    task, baseline, _reference = _load(repo_root)

    class Fake(LLMClient):
        def complete(self, messages, tools):
            return LLMResponse(
                tool_calls=[LLMToolCall(id="1", name="done", args={"summary": "s"})],
                usage=LLMUsage(1200, 400),
            )

    monkeypatch.setattr(
        runner_mod, "_create_agent", lambda *a, **k: LLMAgent(Fake(), model="fake")
    )
    results_path = tmp_path / "results.jsonl"
    outcome = runner_mod.run_single(
        task, baseline, 1, 0, tmp_path / "ws", "exp_tok",
        results_path, repo_root,
    )
    assert outcome["input_tokens"] == 1200
    assert outcome["output_tokens"] == 400
    assert outcome["total_tokens"] == 1600
    assert outcome["token_source"] == "provider"
    results_mod.validate_result(results_mod.read_results(results_path)[0])
