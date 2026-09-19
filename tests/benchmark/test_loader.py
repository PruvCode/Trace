"""Loader tests: valid tasks load; malformed tasks fail loudly."""

from pathlib import Path

import pytest
import yaml

from benchmark import loader as loader_mod


def _write_task(tmp_path: Path, **overrides) -> Path:
    meta = {
        "task_id": "tmp_task",
        "category": "A_control",
        "fixture": "fixtures/toy_repo",
        "base_commit": "abc123",
        "prompt_file": "prompt.md",
        "eval_command": ["python", "-m", "pytest", "tests/test_session.py", "-q"],
        "timeout_seconds": 60,
        "tags": ["tmp"],
    }
    meta.update(overrides)
    task_dir = tmp_path / "tmp_task"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    (task_dir / "prompt.md").write_text("Do the thing.\n", encoding="utf-8")
    return task_dir


def test_load_valid_task(repo_root, seeded_fixture):
    _, head = seeded_fixture
    task = loader_mod.load_task(
        repo_root / "tasks" / "A_control" / "task_01_timeout_fix", repo_root
    )
    assert task.task_id == "task_01_timeout_fix"
    assert task.category == "A_control"
    assert task.base_commit == head
    assert task.prompt and task.prompt_sha256
    assert task.eval_command[0] == "python"
    assert task.task_version


def test_task_fixture_consistency(repo_root, seeded_fixture):
    """Proves the pinned base_commit matches the seeded fixture HEAD."""
    _, head = seeded_fixture
    task = loader_mod.load_task(
        repo_root / "tasks" / "A_control" / "task_01_timeout_fix", repo_root
    )
    assert task.base_commit == head


def test_missing_key_rejected(tmp_path, repo_root):
    task_dir = _write_task(tmp_path)
    meta = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    del meta["tags"]
    (task_dir / "task.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    with pytest.raises(ValueError, match="missing keys"):
        loader_mod.load_task(task_dir, repo_root)


def test_unknown_key_rejected(tmp_path, repo_root):
    task_dir = _write_task(tmp_path, typo_field="oops")
    with pytest.raises(ValueError, match="unknown keys"):
        loader_mod.load_task(task_dir, repo_root)


def test_bad_category_rejected(tmp_path, repo_root):
    task_dir = _write_task(tmp_path, category="Z_nonsense")
    with pytest.raises(ValueError, match="category"):
        loader_mod.load_task(task_dir, repo_root)


def test_load_baseline_config(repo_root):
    cfg = loader_mod.load_config(repo_root / "configs" / "baseline.yaml")
    assert cfg["name"] == "baseline"
    assert cfg["backend"] is None
    assert cfg["agent"] == "mock"
    for key in ("max_turns", "max_tool_calls", "timeout_seconds"):
        assert key in cfg["budget"]
    assert cfg["config_hash"]


def test_config_unknown_key_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "backend": None,
                "agent": "mock",
                "model": "mock",
                "typo": 1,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown keys"):
        loader_mod.load_config(bad)
