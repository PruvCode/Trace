"""Phase 5.4 tests: Category D staleness task (retrieval vs influence)."""

from pathlib import Path

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

D_REF = Path("tasks") / "D_staleness" / "task_01_suffix"
PIN = "a97390a8c7b7f17c90d57598538192aebc6b6ecb"

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
    "memory",
    "stale",
    "histor",
    "token-{user_id}",
    "-fresh",
]

TOKENS_HEAD = '"""Token issuance (history fixture v1)."""\n\n\ndef refresh_token(user_id):\n    """Issue a token for a user."""\n    return f"token-{user_id}-fresh"\n\n\ndef validate_token(token):\n    """Return True for issued tokens."""\n    return token.startswith("token-")\n'

WRONG_PREDICATE = '    return token.startswith("token-") or token == "token-guest"'
RIGHT_PREDICATE = '    return token.startswith("token-") and token.endswith("-fresh")'
CLEAN_PREDICATE = '    return token.startswith("token-")'


def _load(repo_root: Path):
    return loader_mod.load_task(repo_root / D_REF, repo_root)


def _clean_workspace(tmp_path: Path, repo_root: Path, task, name: str):
    fixture_dir = (repo_root / task.fixture).resolve()
    head = workspace_mod.ensure_fixture(fixture_dir)
    assert head == task.base_commit
    return workspace_mod.create_workspace(fixture_dir, head, tmp_path / name)


def test_task_loads(repo_root):
    task = _load(repo_root)
    assert task.task_id == "task_01_suffix"
    assert task.category == "D_staleness"
    assert task.fixture == "fixtures/history_repo"
    assert task.base_commit == PIN
    assert len(task.preseed) == 1
    assert task.preseed[0]["type"] == "observation"
    assert task.preseed[0]["source"] == "agent"


def test_pin_matches_head(repo_root, seeded_history):
    _history_dir, shas = seeded_history
    assert shas[-1] == PIN
    assert _load(repo_root).base_commit == PIN


def test_red(tmp_path, repo_root):
    task = _load(repo_root)
    ws = _clean_workspace(tmp_path, repo_root, task, "ws_d_red")
    result = evaluator_mod.run_evaluator(ws, task.eval_command, task.timeout_seconds)
    assert result.success is False


def test_green(tmp_path, repo_root):
    task = _load(repo_root)
    ws = _clean_workspace(tmp_path, repo_root, task, "ws_d_green")
    target = ws / "auth" / "tokens.py"
    text = target.read_text(encoding="utf-8")
    assert CLEAN_PREDICATE in text
    target.write_text(text.replace(CLEAN_PREDICATE, RIGHT_PREDICATE), encoding="utf-8")
    result = evaluator_mod.run_evaluator(ws, task.eval_command, task.timeout_seconds)
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


def _run_with_script(repo_root, tmp_path, monkeypatch, config, script, exp):
    import benchmark.runner as runner_module

    task = _load(repo_root)

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


def _configs(repo_root: Path):
    baseline = loader_mod.load_config(repo_root / "configs" / "baseline.yaml")
    reference = loader_mod.load_config(repo_root / "configs" / "reference_memory.yaml")
    return baseline, reference


def _db_events(db_path: Path):
    conn = store_mod.connect(db_path)
    try:
        return episodic_mod.search_events(conn)
    finally:
        conn.close()


def test_baseline_absent_reference_present(tmp_path, repo_root, monkeypatch):
    task = _load(repo_root)
    baseline, reference = _configs(repo_root)
    done_script = [("done", {"summary": "s"})]
    base_outcome, _, _ = _run_with_script(
        repo_root, tmp_path, monkeypatch, baseline, done_script, "exp_d_base"
    )
    assert base_outcome["error"] is None
    assert not (Path(base_outcome["workspace"]) / ".agent-memory").exists()
    ref_outcome, _, _ = _run_with_script(
        repo_root, tmp_path, monkeypatch, reference, done_script, "exp_d_ref"
    )
    assert ref_outcome["error"] is None
    rows = _db_events(workspace_db_path(Path(ref_outcome["workspace"])))
    assert len(rows) == 1
    event = rows[0]
    declared = task.preseed[0]
    assert event.type == declared["type"] == "observation"
    assert event.source == declared["source"] == "agent"
    assert event.symbol == declared["symbol"] == "refresh_token"
    assert event.file == declared["file"] == "auth/tokens.py"
    assert event.payload == declared["payload"]
    assert "bare" in event.payload["finding"]


def test_runs_isolated(tmp_path, repo_root, monkeypatch):
    _, reference = _configs(repo_root)
    done_script = [("done", {"summary": "s"})]
    out, _, _ = _run_with_script(
        repo_root, tmp_path, monkeypatch, reference, done_script, "exp_d_iso"
    )
    rows = _db_events(workspace_db_path(Path(out["workspace"])))
    assert [e.symbol for e in rows] == ["refresh_token"]
    payloads = str([e.payload for e in rows])
    assert "v2:token-" not in payloads
    assert all(e.symbol not in ("renew_session", "validate_token") for e in rows)


def test_probe_stale_retrieved(tmp_path, repo_root, monkeypatch):
    _, reference = _configs(repo_root)
    script = [
        ("search_events", {"symbol": "refresh_token"}),
        ("done", {"summary": "s"}),
    ]
    outcome, _, client = _run_with_script(
        repo_root, tmp_path, monkeypatch, reference, script, "exp_d_probe"
    )
    assert outcome["error"] is None
    assert outcome["memory_tool_calls"] == 1
    transcript = str(client.seen_messages)
    assert "bare" in transcript
    assert "no suffix" in transcript


def test_probe_git_corrorborates(tmp_path, repo_root, monkeypatch):
    _, reference = _configs(repo_root)
    script = [
        ("get_git_history", {"path": "auth/tokens.py"}),
        ("done", {"summary": "s"}),
    ]
    outcome, _, client = _run_with_script(
        repo_root, tmp_path, monkeypatch, reference, script, "exp_d_git"
    )
    assert outcome["error"] is None
    transcript = str(client.seen_messages)
    assert "wire-up" in transcript


def test_tokens_head_matches_fixture(seeded_history):
    """Drift guard: scripted file contents must equal the fixture bytes."""
    history_dir, _shas = seeded_history
    actual = (history_dir / "auth" / "tokens.py").read_text(encoding="utf-8")
    assert TOKENS_HEAD == actual


def test_influence_fail_on_stale_action(tmp_path, repo_root, monkeypatch):
    """Constructed demonstration, not causal proof: a scripted agent that
    codifies the stale belief produces a red evaluator result."""
    _, reference = _configs(repo_root)
    wrong_file = TOKENS_HEAD.replace(CLEAN_PREDICATE, WRONG_PREDICATE)
    script = [
        ("search_events", {"symbol": "refresh_token"}),
        ("write_file", {"path": "auth/tokens.py", "content": wrong_file}),
        ("done", {"summary": "s"}),
    ]
    outcome, _, _ = _run_with_script(
        repo_root, tmp_path, monkeypatch, reference, script, "exp_d_infl_fail"
    )
    assert outcome["error"] is None
    assert outcome["memory_tool_calls"] == 1
    assert outcome["success"] is False


def test_influence_pass_on_verify_then_fix(tmp_path, repo_root, monkeypatch):
    """Retrieval without blind trust is legitimate: verify against current
    code, then fix correctly."""
    _, reference = _configs(repo_root)
    right_file = TOKENS_HEAD.replace(CLEAN_PREDICATE, RIGHT_PREDICATE)
    script = [
        ("search_events", {"symbol": "refresh_token"}),
        ("read_file", {"path": "auth/tokens.py"}),
        ("write_file", {"path": "auth/tokens.py", "content": right_file}),
        ("done", {"summary": "s"}),
    ]
    outcome, _, _ = _run_with_script(
        repo_root, tmp_path, monkeypatch, reference, script, "exp_d_infl_pass"
    )
    assert outcome["error"] is None
    assert outcome["success"] is True


def test_fairness_holds(repo_root):
    baseline, reference = _configs(repo_root)
    task = _load(repo_root)
    base_spec = fairness_mod.resolve_run_spec(task, baseline, str(repo_root))
    ref_spec = fairness_mod.resolve_run_spec(task, reference, str(repo_root))
    assert "fair" in fairness_mod.assert_fair(base_spec, ref_spec)
    assert base_spec["prompt_sha256"] == ref_spec["prompt_sha256"]


def test_prompt_leakage(repo_root):
    prompt = (repo_root / D_REF / "prompt.md").read_text(encoding="utf-8")
    lowered = prompt.lower()
    for token in BANNED_PROMPT_TOKENS:
        assert token.lower() not in lowered, token


def test_results_schema_valid(tmp_path, repo_root, monkeypatch):
    baseline, _reference = _configs(repo_root)
    done_script = [("done", {"summary": "s"})]
    _outcome, results_path, _client = _run_with_script(
        repo_root, tmp_path, monkeypatch, baseline, done_script, "exp_d_schema"
    )
    for row in results_mod.read_results(results_path):
        results_mod.validate_result(row)
