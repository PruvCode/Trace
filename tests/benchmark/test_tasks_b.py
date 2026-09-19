"""Phase 5.2 tests: Category B structural tasks (determinism → fairness)."""

from pathlib import Path

from agent.llm_agent import LLMAgent, LLMClient, LLMResponse, LLMToolCall
from benchmark import evaluator as evaluator_mod
from benchmark import fairness as fairness_mod
from benchmark import loader as loader_mod
from benchmark import results as results_mod
from benchmark import runner as runner_mod
from benchmark import workspace as workspace_mod
from memory import store as store_mod
from memory import structural as structural_mod

B1_REF = Path("tasks") / "B_structural" / "task_01_callers"
B2_REF = Path("tasks") / "B_structural" / "task_02_definition"

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
    "validate_token",
    "TOKEN_TTL_SECONDS",
    "SESSION_TTL_SECONDS",
    "is_within_ttl",
    "services.py",
    "policy.py",
    "tokens.py",
    "handlers.py",
]


def _load_both(repo_root: Path):
    b1 = loader_mod.load_task(repo_root / B1_REF, repo_root)
    b2 = loader_mod.load_task(repo_root / B2_REF, repo_root)
    return b1, b2


def _clean_workspace(tmp_path: Path, repo_root: Path, task, name: str):
    fixture_dir = (repo_root / task.fixture).resolve()
    head = workspace_mod.ensure_fixture(fixture_dir)
    assert head == task.base_commit
    return workspace_mod.create_workspace(fixture_dir, head, tmp_path / name)


def test_fixture_determinism(seeded_deps):
    _deps_dir, head = seeded_deps
    from scripts.seed_deps_repo import ensure_seeded_deps

    assert ensure_seeded_deps(_deps_dir) == head


def test_tasks_load(repo_root):
    b1, b2 = _load_both(repo_root)
    assert (b1.task_id, b1.category) == ("task_01_callers", "B_structural")
    assert (b2.task_id, b2.category) == ("task_02_definition", "B_structural")
    for task_ref in (B1_REF, B2_REF):
        raw = (repo_root / task_ref / "task.yaml").read_text(encoding="utf-8")
        assert "preseed" not in raw.lower()


def test_pins_match_seed(repo_root, seeded_deps):
    _deps_dir, head = seeded_deps
    b1, b2 = _load_both(repo_root)
    assert b1.base_commit == head
    assert b2.base_commit == head


def test_b1_red(tmp_path, repo_root):
    b1, _b2 = _load_both(repo_root)
    ws = _clean_workspace(tmp_path, repo_root, b1, "ws_b1_red")
    result = evaluator_mod.run_evaluator(ws, b1.eval_command, b1.timeout_seconds)
    assert result.success is False


def test_b1_green(tmp_path, repo_root):
    b1, _b2 = _load_both(repo_root)
    ws = _clean_workspace(tmp_path, repo_root, b1, "ws_b1_green")
    target = ws / "services.py"
    text = target.read_text(encoding="utf-8")
    old = (
        '    if not isinstance(token, str) or not token.startswith("tok_"):\n'
        '        raise ValueError("bad token")'
    )
    assert old in text
    text = text.replace(
        old,
        "    if not validate_token(token):\n" '        raise ValueError("bad token")',
    )
    text = text.replace(
        '"""Session services: renewal and expiry checks."""',
        '"""Session services: renewal and expiry checks."""\n\n'
        "from tokens import validate_token",
    )
    target.write_text(text, encoding="utf-8")
    result = evaluator_mod.run_evaluator(ws, b1.eval_command, b1.timeout_seconds)
    assert result.success is True


def test_b2_red(tmp_path, repo_root):
    _b1, b2 = _load_both(repo_root)
    ws = _clean_workspace(tmp_path, repo_root, b2, "ws_b2_red")
    result = evaluator_mod.run_evaluator(ws, b2.eval_command, b2.timeout_seconds)
    assert result.success is False


def test_b2_green(tmp_path, repo_root):
    _b1, b2 = _load_both(repo_root)
    ws = _clean_workspace(tmp_path, repo_root, b2, "ws_b2_green")
    target = ws / "services.py"
    text = target.read_text(encoding="utf-8")
    assert "SESSION_TTL_SECONDS = 1800" in text
    text = text.replace(
        "SESSION_TTL_SECONDS = 1800", "from policy import TOKEN_TTL_SECONDS"
    )
    text = text.replace(
        "return elapsed_seconds < SESSION_TTL_SECONDS",
        "return elapsed_seconds < TOKEN_TTL_SECONDS",
    )
    target.write_text(text, encoding="utf-8")
    result = evaluator_mod.run_evaluator(ws, b2.eval_command, b2.timeout_seconds)
    assert result.success is True


def test_structural_facts(tmp_path, seeded_deps):
    deps_dir, _head = seeded_deps
    db_path = tmp_path / "memory.db"
    stats = structural_mod.index_workspace(deps_dir, db_path)
    assert stats["symbols"] > 0
    conn = store_mod.connect(db_path)
    try:
        definitions = store_mod.find_definition(conn, "is_within_ttl")
        assert len(definitions) == 1
        assert definitions[0]["file"] == "policy.py"
        callers = store_mod.find_callers(conn, "validate_token")
        assert {c["caller"] for c in callers} >= {"handle_login"}
        assert all(set(c) == {"caller", "file", "line"} for c in callers)
        assert len(store_mod.search_symbols(conn, "renew", limit=5)) <= 5
    finally:
        conn.close()


class _ScriptedClient(LLMClient):
    """Plays a fixed tool sequence, then done. No network, no model."""

    def __init__(self, calls):
        self._calls = list(calls)

    def complete(self, messages, tools):
        _ = (messages, tools)
        name, args = self._calls.pop(0)
        return LLMResponse(
            tool_calls=[LLMToolCall(id="1", name=name, args=args)]
        )


def _run_with_script(repo_root, tmp_path, monkeypatch, task_ref, config_name, script, exp):
    import benchmark.runner as runner_module

    task = loader_mod.load_task(repo_root / task_ref, repo_root)
    config = loader_mod.load_config(repo_root / "configs" / f"{config_name}.yaml")

    def _agent(*args, **kwargs):
        _ = (args, kwargs)
        return LLMAgent(_ScriptedClient(script), model="fake")

    results_path = tmp_path / "results.jsonl"
    monkeypatch.setattr(runner_module, "_create_agent", _agent)
    return runner_mod.run_single(
        task, config, 1, 0, tmp_path / "ws", exp,
        results_path, repo_root,
    ), results_path


def test_both_configs_execute_b1(tmp_path, repo_root, seeded_deps, monkeypatch):
    _deps_dir, _head = seeded_deps
    base_script = [("read_file", {"path": "services.py"}), ("done", {"summary": "s"})]
    mem_script = [
        ("find_callers", {"name": "validate_token"}),
        ("done", {"summary": "s"}),
    ]
    base_outcome, _ = _run_with_script(
        repo_root, tmp_path, monkeypatch, B1_REF, "baseline", base_script, "exp_b1_base"
    )
    assert base_outcome["configuration"] == "baseline"
    assert base_outcome["error"] is None
    assert base_outcome["memory_tool_calls"] == 0
    ref_outcome, _ = _run_with_script(
        repo_root, tmp_path, monkeypatch, B1_REF, "reference_memory", mem_script, "exp_b1_ref"
    )
    assert ref_outcome["configuration"] == "reference_memory"
    assert ref_outcome["error"] is None
    assert ref_outcome["memory_tool_calls"] == 1


def test_both_configs_execute_b2(tmp_path, repo_root, seeded_deps, monkeypatch):
    _deps_dir, _head = seeded_deps
    base_script = [("read_file", {"path": "services.py"}), ("done", {"summary": "s"})]
    mem_script = [
        ("find_definition", {"name": "is_within_ttl"}),
        ("done", {"summary": "s"}),
    ]
    base_outcome, _ = _run_with_script(
        repo_root, tmp_path, monkeypatch, B2_REF, "baseline", base_script, "exp_b2_base"
    )
    assert base_outcome["error"] is None
    ref_outcome, results_path = _run_with_script(
        repo_root, tmp_path, monkeypatch, B2_REF, "reference_memory", mem_script, "exp_b2_ref"
    )
    assert ref_outcome["memory_tool_calls"] == 1
    for row in results_mod.read_results(results_path):
        results_mod.validate_result(row)


def test_fairness_holds_per_b_task(repo_root):
    baseline = loader_mod.load_config(repo_root / "configs" / "baseline.yaml")
    reference = loader_mod.load_config(repo_root / "configs" / "reference_memory.yaml")
    for task_ref in (B1_REF, B2_REF):
        task = loader_mod.load_task(repo_root / task_ref, repo_root)
        base_spec = fairness_mod.resolve_run_spec(task, baseline, str(repo_root))
        ref_spec = fairness_mod.resolve_run_spec(task, reference, str(repo_root))
        assert "fair" in fairness_mod.assert_fair(base_spec, ref_spec)


def test_prompt_leakage(repo_root):
    for task_ref in (B1_REF, B2_REF):
        prompt = (repo_root / task_ref / "prompt.md").read_text(encoding="utf-8")
        lowered = prompt.lower()
        for token in BANNED_PROMPT_TOKENS:
            assert token.lower() not in lowered, f"{task_ref}: {token}"
