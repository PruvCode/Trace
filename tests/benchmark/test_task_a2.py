"""Phase 5.1 tests: control task A2 (red/green, fairness, no leakage)."""

from pathlib import Path

from benchmark import evaluator as evaluator_mod
from benchmark import loader as loader_mod
from benchmark import runner as runner_mod
from benchmark import workspace as workspace_mod

TASK_REF = Path("tasks") / "A_control" / "task_02_empty_user"

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
    "if not user_id",
    "unknown user",
]


def _load(repo_root: Path):
    return loader_mod.load_task(repo_root / TASK_REF, repo_root)


def _clean_workspace(tmp_path: Path, repo_root: Path):
    task = _load(repo_root)
    fixture_dir = (repo_root / task.fixture).resolve()
    head = workspace_mod.ensure_fixture(fixture_dir)
    assert head == task.base_commit
    return task, workspace_mod.create_workspace(fixture_dir, head, tmp_path / "ws")


def test_a2_loads_and_shares_pin(repo_root, seeded_fixture):
    _fixture_dir, head = seeded_fixture
    task = _load(repo_root)
    assert task.task_id == "task_02_empty_user"
    assert task.category == "A_control"
    assert task.base_commit == head
    # Same fixture state as task_01: no fixture churn in 5.1.
    task_01 = loader_mod.load_task(
        repo_root / "tasks" / "A_control" / "task_01_timeout_fix", repo_root
    )
    assert task.base_commit == task_01.base_commit


def test_a2_red_on_clean(tmp_path, repo_root):
    task, ws = _clean_workspace(tmp_path, repo_root)
    result = evaluator_mod.run_evaluator(ws, task.eval_command, task.timeout_seconds)
    assert result.success is False


def test_a2_green_on_reference_patch(tmp_path, repo_root):
    task, ws = _clean_workspace(tmp_path, repo_root)
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
    result = evaluator_mod.run_evaluator(ws, task.eval_command, task.timeout_seconds)
    assert result.success is True


def test_a2_prompt_has_no_leakage(repo_root):
    prompt = (repo_root / TASK_REF / "prompt.md").read_text(encoding="utf-8")
    lowered = prompt.lower()
    for token in BANNED_PROMPT_TOKENS:
        assert token.lower() not in lowered, token
    assert "ValueError" in prompt  # the requirement itself must be stated


def test_a2_executes_both_configs(tmp_path, repo_root):
    task = _load(repo_root)
    baseline = loader_mod.load_config(repo_root / "configs" / "baseline.yaml")
    reference = loader_mod.load_config(repo_root / "configs" / "reference_memory.yaml")
    base_outcome = runner_mod.run_single(
        task, baseline, 1, 0, tmp_path / "ws", "exp_a2",
        tmp_path / "results.jsonl", repo_root, mock_behavior_override="pass",
    )
    assert base_outcome["configuration"] == "baseline"
    assert base_outcome["error"] is None
    assert base_outcome["termination_reason"] == "done_tool_called"
    ref_outcome = runner_mod.run_single(
        task, reference, 1, 0, tmp_path / "ws", "exp_a2",
        tmp_path / "results.jsonl", repo_root,
    )
    assert ref_outcome["configuration"] == "reference_memory"
    assert ref_outcome["error"] is None
    assert ref_outcome["memory_tool_calls"] == 2


def test_pilot_skeleton_labelled_and_delegating(tmp_path, repo_root, capsys):
    import json

    from scripts import pilot as pilot_mod

    source = (repo_root / "scripts" / "pilot.py").read_text(encoding="utf-8")
    assert "from benchmark.runner import main as run_benchmark" in source
    assert "def run_evaluator" not in source and "subprocess" not in source

    runs_file = tmp_path / "pilot.jsonl"
    code = pilot_mod.main(
        [
            "--tasks-root",
            str(repo_root / "tasks" / "A_control"),
            "--config",
            str(repo_root / "configs" / "baseline.yaml"),
            "--work-root",
            str(tmp_path / "ws"),
            "--exp-id",
            "pilot_test",
            "--runs-file",
            str(runs_file),
            "--mock-behavior",
            "pass",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "PILOT" in out
    rows = [json.loads(line) for line in runs_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 6  # all A tasks ran
    assert {row["task_id"] for row in rows} == {
        "task_01_timeout_fix",
        "task_02_empty_user",
        "task_03_token_prefix",
        "task_04_page_offbyone",
        "task_05_retry_count",
        "task_06_csv_header",
    }
