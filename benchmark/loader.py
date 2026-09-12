"""Task + config loading with strict validation.

Unknown task.yaml keys are rejected so typos fail loudly instead of silently
changing an experiment.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import yaml

from benchmark.schemas import TaskConfig

CATEGORIES = {"A_control", "B_structural", "C_episodic", "D_staleness"}

TASK_FIELDS = {
    "task_id",
    "category",
    "fixture",
    "base_commit",
    "prompt_file",
    "eval_command",
    "timeout_seconds",
    "tags",
}

CONFIG_FIELDS = {
    "name",
    "backend",
    "agent",
    "model",
    "model_parameters",
    "budget",
    "mock",
    "prompt_addendum",
    "memory",
}


def _sha_files(paths: list[Path]) -> str:
    digest = sha256()
    for path in sorted(paths):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def task_version_for(task_dir: Path) -> str:
    """Content hash over task.yaml + prompt.md + tests/** (if present)."""
    files = [task_dir / "task.yaml"]
    prompt_name = "prompt.md"
    try:
        meta = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
        if isinstance(meta, dict) and isinstance(meta.get("prompt_file"), str):
            prompt_name = meta["prompt_file"]
    except OSError:
        pass
    files.append(task_dir / prompt_name)
    tests_dir = task_dir / "tests"
    if tests_dir.is_dir():
        files.extend(p for p in tests_dir.rglob("*") if p.is_file())
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"task files missing: {missing}")
    return _sha_files(files)


def load_task(task_dir: Path, repo_root: Path) -> TaskConfig:
    """Load and validate tasks/<cat>/<id>/task.yaml."""
    task_dir = task_dir.resolve()
    meta_path = task_dir / "task.yaml"
    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise ValueError(f"{meta_path} must contain a mapping")
    unknown = set(meta) - TASK_FIELDS
    if unknown:
        raise ValueError(f"{meta_path} has unknown keys: {sorted(unknown)}")
    missing = TASK_FIELDS - set(meta)
    if missing:
        raise ValueError(f"{meta_path} is missing keys: {sorted(missing)}")

    if not isinstance(meta["task_id"], str) or not meta["task_id"]:
        raise ValueError("task_id must be a non-empty string")
    if meta["category"] not in CATEGORIES:
        raise ValueError(f"category must be one of {sorted(CATEGORIES)}")
    if not isinstance(meta["fixture"], str) or not meta["fixture"]:
        raise ValueError("fixture must be a repo-relative directory string")
    if not isinstance(meta["base_commit"], str) or not meta["base_commit"]:
        raise ValueError("base_commit must be a non-empty git SHA string")
    if not isinstance(meta["prompt_file"], str) or not meta["prompt_file"]:
        raise ValueError("prompt_file must be a non-empty string")
    cmd = meta["eval_command"]
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
        raise ValueError("eval_command must be a non-empty list of strings")
    if not isinstance(meta["timeout_seconds"], int) or meta["timeout_seconds"] <= 0:
        raise ValueError("timeout_seconds must be a positive int")
    if not isinstance(meta["tags"], list) or not all(
        isinstance(t, str) for t in meta["tags"]
    ):
        raise ValueError("tags must be a list of strings")

    fixture_dir = repo_root / meta["fixture"]
    if not fixture_dir.is_dir():
        raise FileNotFoundError(f"fixture directory missing: {fixture_dir}")
    prompt_path = task_dir / meta["prompt_file"]
    if not prompt_path.is_file():
        raise FileNotFoundError(f"prompt file missing: {prompt_path}")
    prompt = prompt_path.read_text(encoding="utf-8")
    prompt_sha = sha256(prompt.encode("utf-8")).hexdigest()

    return TaskConfig(
        task_id=meta["task_id"],
        category=meta["category"],
        fixture=meta["fixture"],
        base_commit=meta["base_commit"],
        prompt_file=meta["prompt_file"],
        eval_command=list(cmd),
        timeout_seconds=meta["timeout_seconds"],
        tags=list(meta["tags"]),
        task_dir=str(task_dir.relative_to(repo_root.resolve())),
        task_version=task_version_for(task_dir),
        prompt_sha256=prompt_sha,
        prompt=prompt,
    )


def load_config(config_path: Path) -> dict:
    """Load and validate a configs/*.yaml file (Phase 1: mock agent only)."""
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError(f"{config_path} must contain a mapping")
    unknown = set(cfg) - CONFIG_FIELDS
    if unknown:
        raise ValueError(f"{config_path} has unknown keys: {sorted(unknown)}")
    for key in ("name", "backend", "agent", "model"):
        if key not in cfg:
            raise ValueError(f"{config_path} is missing key: {key}")
    budget = cfg.get("budget") or {}
    for key in ("max_turns", "max_tool_calls", "timeout_seconds"):
        if key not in budget:
            raise ValueError(f"{config_path} budget is missing key: {key}")
    if "memory" in cfg:
        if not isinstance(cfg["memory"], dict):
            raise ValueError(f"{config_path} memory must be a mapping")
        preseed = cfg["memory"].get("preseed", [])
        if not isinstance(preseed, list) or not all(
            isinstance(e, dict) for e in preseed
        ):
            raise ValueError(f"{config_path} memory.preseed must be a list of dicts")
    cfg.setdefault("model_parameters", {})
    cfg.setdefault("mock", {"behavior": "pass"})
    cfg.setdefault("prompt_addendum", "")
    raw = config_path.read_bytes()
    cfg["config_hash"] = sha256(raw).hexdigest()
    cfg["config_path"] = str(config_path)
    return cfg
