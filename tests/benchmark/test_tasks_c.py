"""Phase 5.3 tests: task preseed plumbing + Category C episodic tasks."""

from pathlib import Path

import pytest
import yaml

from agent.llm_agent import LLMAgent, LLMClient, LLMResponse, LLMToolCall
from benchmark import evaluator as evaluator_mod
from benchmark import fairness as fairness_mod
from benchmark import loader as loader_mod
from benchmark import results as results_mod
from benchmark import runner as runner_mod
from benchmark import workspace as workspace_mod
from memory import episodic as episodic_mod
from memory import store as store_mod
from memory.reference import workspace_db_path

C1_REF = Path("tasks") / "C_episodic" / "task_01_failed_attempt"
C2_REF = Path("tasks") / "C_episodic" / "task_02_decision"

BANNED_PROMPT_TOKENS = [
    "find_definition",
    "find_callers",
    "search_symbols",
    "record_event",
    "search_events",
    "get_git_history",
    "tree-sitter",
    "sqlite",
    "fts",
    "evaluator",
    "task.yaml",
    "preseed",
    "v2:",
    "service IDs",
    "cross-user",
]


def _load_both(repo_root: Path):
    c1 = loader_mod.load_task(repo_root / C1_REF, repo_root)
    c2 = loader_mod.load_task(repo_root / C2_REF, repo_root)
    return c1, c2


def _clean_workspace(tmp_path: Path, repo_root: Path, task, name: str):
    fixture_dir = (repo_root / task.fixture).resolve()
    head = workspace_mod.ensure_fixture(fixture_dir)
    assert head == task.base_commit
    return workspace_mod.create_workspace(fixture_dir, head, tmp_path / name)


def test_preseed_defaults_to_empty(repo_root):
    task = loader_mod.load_task(
        repo_root / "tasks" / "A_control" / "task_01_timeout_fix", repo_root
    )
    assert task.preseed == []


def test_preseed_invalid_rejected(tmp_path):
    meta = {
        "task_id": "bad",
        "category": "C_episodic",
        "fixture": "fixtures/history_repo",
        "base_commit": "abc123",
        "prompt_file": "prompt.md",
        "eval_command": ["python", "-c", "pass"],
        "timeout_seconds": 60,
        "tags": [],
    }
    task_dir = tmp_path / "bad_task"
    task_dir.mkdir()
    (task_dir / "prompt.md").write_text("Do the thing.\n", encoding="utf-8")
    (tmp_path / "fixtures" / "history_repo").mkdir(parents=True)
    for bad_preseed in ("not-a-list", ["not-a-dict"], [{"type": "x"}]):
        meta["preseed"] = bad_preseed
        (task_dir / "task.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
        if isinstance(bad_preseed, list) and all(
            isinstance(e, dict) for e in bad_preseed
        ):
            task = loader_mod.load_task(task_dir, tmp_path)
            assert task.preseed == bad_preseed
        else:
            with pytest.raises(ValueError, match="preseed"):
                loader_mod.load_task(task_dir, tmp_path)


def test_preseed_changes_task_version(tmp_path):
    base = {
        "task_id": "v",
        "category": "C_episodic",
        "fixture": "fixtures/history_repo",
        "base_commit": "abc123",
        "prompt_file": "prompt.md",
        "eval_command": ["python", "-c", "pass"],
        "timeout_seconds": 60,
        "tags": [],
    }
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    (plain_dir / "prompt.md").write_text("Do the thing.\n", encoding="utf-8")
    (plain_dir / "task.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    seeded_dir = tmp_path / "seeded"
    seeded_dir.mkdir()
    (seeded_dir / "prompt.md").write_text("Do the thing.\n", encoding="utf-8")
    seeded_meta = dict(base)
    seeded_meta["preseed"] = [
        {
            "type": "observation",
            "source": "agent",
            "timestamp": "2026-02-01T00:00:00+00:00",
            "repo": "history_repo",
            "payload": {"finding": "x"},
        }
    ]
    (seeded_dir / "task.yaml").write_text(
        yaml.safe_dump(seeded_meta), encoding="utf-8"
    )
    assert loader_mod.task_version_for(plain_dir) != loader_mod.task_version_for(
        seeded_dir
    )


def test_c_tasks_load(repo_root):
    c1, c2 = _load_both(repo_root)
    assert (c1.task_id, c1.category) == ("task_01_failed_attempt", "C_episodic")
    assert (c2.task_id, c2.category) == ("task_02_decision", "C_episodic")
    assert len(c1.preseed) == 1 and len(c2.preseed) == 1
    assert c1.preseed[0]["type"] == "attempt"
    assert c1.preseed[0]["source"] == "agent"
    assert c2.preseed[0]["type"] == "decision"
    assert c2.preseed[0]["source"] == "agent"


def test_history_pin_stable(repo_root, seeded_history):
    _history_dir, shas = seeded_history
    assert len(shas) == 3
    c1, c2 = _load_both(repo_root)
    assert c1.base_commit == shas[-1]
    assert c2.base_commit == shas[-1]


def test_c1_red(tmp_path, repo_root):
    c1, _c2 = _load_both(repo_root)
    ws = _clean_workspace(tmp_path, repo_root, c1, "ws_c1_red")
    result = evaluator_mod.run_evaluator(ws, c1.eval_command, c1.timeout_seconds)
    assert result.success is False


def test_c1_green(tmp_path, repo_root):
    c1, _c2 = _load_both(repo_root)
    ws = _clean_workspace(tmp_path, repo_root, c1, "ws_c1_green")
    target = ws / "auth" / "login.py"
    text = target.read_text(encoding="utf-8")
    old = '    if password != "s3cret":'
    assert old in text
    target.write_text(
        text.replace(
            old,
            '    if not user_id:\n        raise ValueError("unknown user")\n'
            '    if password != "s3cret":',
        ),
        encoding="utf-8",
    )
    result = evaluator_mod.run_evaluator(ws, c1.eval_command, c1.timeout_seconds)
    assert result.success is True


def test_c2_red(tmp_path, repo_root):
    _c1, c2 = _load_both(repo_root)
    ws = _clean_workspace(tmp_path, repo_root, c2, "ws_c2_red")
    result = evaluator_mod.run_evaluator(ws, c2.eval_command, c2.timeout_seconds)
    assert result.success is False


def test_c2_green(tmp_path, repo_root):
    _c1, c2 = _load_both(repo_root)
    ws = _clean_workspace(tmp_path, repo_root, c2, "ws_c2_green")
    target = ws / "auth" / "tokens.py"
    text = target.read_text(encoding="utf-8")
    old = '    return token.startswith("token-")'
    assert old in text
    target.write_text(
        text.replace(
            old,
            '    return token.startswith("token-") or token.startswith("v2:token-")',
        ),
        encoding="utf-8",
    )
    result = evaluator_mod.run_evaluator(ws, c2.eval_command, c2.timeout_seconds)
    assert result.success is True


class _ScriptedClient(LLMClient):
    """Plays a fixed tool sequence, then done. No network, no model."""

    def __init__(self, calls):
        self._calls = list(calls)
        self.seen_messages: list = []

    def complete(self, messages, tools):
        _ = tools
        self.seen_messages.extend(messages)
        name, args = self._calls.pop(0)
        return LLMResponse(
            tool_calls=[LLMToolCall(id="1", name=name, args=args)]
        )


def _run_with_script(repo_root, tmp_path, monkeypatch, task_ref, config, script, exp):
    import benchmark.runner as runner_module

    task = loader_mod.load_task(repo_root / task_ref, repo_root)

    client = _ScriptedClient(script)

    def _agent(*args, **kwargs):
        _ = (args, kwargs)
        return LLMAgent(client, model="fake")

    results_path = tmp_path / "results.jsonl"
    monkeypatch.setattr(runner_module, "_create_agent", _agent)
    outcome = runner_mod.run_single(
        task, config, 1, 0, tmp_path / "ws", exp,
        results_path, repo_root,
    )
    return outcome, results_path, client


def _db_events(db_path: Path):
    conn = store_mod.connect(db_path)
    try:
        return episodic_mod.search_events(conn)
    finally:
        conn.close()


def _configs(repo_root: Path):
    baseline = loader_mod.load_config(repo_root / "configs" / "baseline.yaml")
    reference = loader_mod.load_config(repo_root / "configs" / "reference_memory.yaml")
    return baseline, reference


def test_baseline_absent_reference_present(tmp_path, repo_root, monkeypatch):
    c1, _c2 = _load_both(repo_root)
    baseline, reference = _configs(repo_root)
    done_script = [("done", {"summary": "s"})]
    base_outcome, _, _ = _run_with_script(
        repo_root, tmp_path, monkeypatch, C1_REF, baseline, done_script, "exp_c1_base"
    )
    assert base_outcome["error"] is None
    assert not (Path(base_outcome["workspace"]) / ".agent-memory").exists()
    ref_outcome, _, _ = _run_with_script(
        repo_root, tmp_path, monkeypatch, C1_REF,
        reference, done_script, "exp_c1_ref",
    )
    assert ref_outcome["error"] is None
    rows = _db_events(workspace_db_path(Path(ref_outcome["workspace"])))
    assert len(rows) == 1
    event = rows[0]
    declared = c1.preseed[0]
    assert event.type == declared["type"] == "attempt"
    assert event.source == declared["source"] == "agent"
    assert event.repo == declared["repo"]
    assert event.symbol == declared["symbol"]
    assert event.file == declared["file"]
    assert event.payload == declared["payload"]


def test_merge_order_task_then_config(tmp_path, repo_root, monkeypatch):
    import copy

    c1, _c2 = _load_both(repo_root)
    _, reference = _configs(repo_root)
    reference = copy.deepcopy(reference)
    reference["memory"] = {
        "preseed": [
            {
                "type": "observation",
                "source": "agent",
                "timestamp": "2026-02-20T00:00:00+00:00",
                "repo": "history_repo",
                "payload": {"finding": "config-level note"},
            }
        ]
    }
    done_script = [("done", {"summary": "s"})]
    outcome, _, _ = _run_with_script(
        repo_root, tmp_path, monkeypatch, C1_REF,
        reference, done_script, "exp_merge",
    )
    assert outcome["error"] is None
    rows = _db_events(workspace_db_path(Path(outcome["workspace"])))
    assert [e.id for e in rows] == [1, 2]
    assert rows[0].payload == c1.preseed[0]["payload"]
    assert rows[1].payload == {"finding": "config-level note"}


def test_runs_isolated(tmp_path, repo_root, monkeypatch):
    c1, c2 = _load_both(repo_root)
    _, reference = _configs(repo_root)
    done_script = [("done", {"summary": "s"})]
    out1, _, _ = _run_with_script(
        repo_root, tmp_path, monkeypatch, C1_REF,
        reference, done_script, "exp_iso1",
    )
    out2, _, _ = _run_with_script(
        repo_root, tmp_path, monkeypatch, C2_REF,
        reference, done_script, "exp_iso2",
    )
    rows1 = _db_events(workspace_db_path(Path(out1["workspace"])))
    rows2 = _db_events(workspace_db_path(Path(out2["workspace"])))
    assert [e.symbol for e in rows1] == ["renew_session"]
    assert [e.symbol for e in rows2] == ["validate_token"]
    assert c1.preseed[0]["payload"] not in [e.payload for e in rows2]


def test_probe_search_events_c1(tmp_path, repo_root, monkeypatch):
    _, reference = _configs(repo_root)
    script = [
        ("search_events", {"symbol": "renew_session"}),
        ("done", {"summary": "s"}),
    ]
    outcome, _, client = _run_with_script(
        repo_root, tmp_path, monkeypatch, C1_REF,
        reference, script, "exp_probe1",
    )
    assert outcome["error"] is None
    assert outcome["memory_tool_calls"] == 1
    transcript = str(client.seen_messages)
    assert "batch renewal flows" in transcript
    assert "login layer" in transcript


def test_probe_search_events_c2_and_git(tmp_path, repo_root, monkeypatch):
    _, reference = _configs(repo_root)
    script = [
        ("search_events", {"symbol": "validate_token"}),
        ("get_git_history", {"symbol": "refresh_token"}),
        ("done", {"summary": "s"}),
    ]
    outcome, _, client = _run_with_script(
        repo_root, tmp_path, monkeypatch, C2_REF,
        reference, script, "exp_probe2",
    )
    assert outcome["error"] is None
    assert outcome["memory_tool_calls"] == 2
    transcript = str(client.seen_messages)
    assert "v2:token-" in transcript
    assert "wire-up" in transcript


def test_probe_content_matches_preseed(tmp_path, repo_root):
    c1, _c2 = _load_both(repo_root)
    db_path = tmp_path / "probe.db"
    conn = store_mod.connect(db_path)
    store_mod.init_schema(conn)
    try:
        from memory import episodic as episodic_module

        for entry in c1.preseed:
            episodic_module.record_event(conn, **entry)
        rows = episodic_mod.search_events(conn, symbol="renew_session")
        assert len(rows) == 1
        assert rows[0].payload["action"] == c1.preseed[0]["payload"]["action"]
        assert rows[0].source == "agent"
        git_rows = episodic_mod.search_events(conn, symbol="refresh_token")
        assert git_rows == []
    finally:
        conn.close()


def test_fairness_holds_per_c_task(repo_root):
    baseline = loader_mod.load_config(repo_root / "configs" / "baseline.yaml")
    reference = loader_mod.load_config(repo_root / "configs" / "reference_memory.yaml")
    for task_ref in (C1_REF, C2_REF):
        task = loader_mod.load_task(repo_root / task_ref, repo_root)
        base_spec = fairness_mod.resolve_run_spec(task, baseline, str(repo_root))
        ref_spec = fairness_mod.resolve_run_spec(task, reference, str(repo_root))
        assert "fair" in fairness_mod.assert_fair(base_spec, ref_spec)
        assert base_spec["prompt_sha256"] == ref_spec["prompt_sha256"]


def test_prompt_leakage(repo_root):
    for task_ref in (C1_REF, C2_REF):
        prompt = (repo_root / task_ref / "prompt.md").read_text(encoding="utf-8")
        lowered = prompt.lower()
        for token in BANNED_PROMPT_TOKENS:
            assert token.lower() not in lowered, f"{task_ref}: {token}"


def test_results_schema_valid(tmp_path, repo_root, monkeypatch):
    baseline, _reference = _configs(repo_root)
    done_script = [("done", {"summary": "s"})]
    _outcome, results_path, _client = _run_with_script(
        repo_root, tmp_path, monkeypatch, C1_REF, baseline, done_script, "exp_schema"
    )
    for row in results_mod.read_results(results_path):
        results_mod.validate_result(row)
