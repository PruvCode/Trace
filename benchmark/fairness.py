"""Fairness invariant: everything identical except the memory capability.

Controls (must be equal): repository, starting commit, task, prompt base,
agent, model, model parameters, tool budget, timeout, evaluation, environment.
Intervention (allowed to differ): memory backend + memory tools + fixed prompt
addendum. Phase 1 has a single baseline config; this module gates the
comparison seam from day one and is covered by trip-tests.
"""

from __future__ import annotations

import platform
import sys

from benchmark.schemas import TaskConfig

CONTROL_FIELDS = [
    "repository",
    "base_commit",
    "task_id",
    "task_version",
    "prompt_sha256",
    "agent",
    "model",
    "model_parameters",
    "budget",
    "eval_timeout_seconds",
    "eval_command",
    "environment",
]


class FairnessError(AssertionError):
    pass


def current_environment() -> str:
    return f"{platform.system()}-{platform.machine()}-py{sys.version_info[0]}.{sys.version_info[1]}"


def resolve_run_spec(task: TaskConfig, config: dict, repo_root: str) -> dict:
    _ = repo_root
    return {
        "repository": task.fixture,
        "base_commit": task.base_commit,
        "task_id": task.task_id,
        "task_version": task.task_version,
        "prompt_sha256": task.prompt_sha256,
        "agent": config["agent"],
        "model": config["model"],
        "model_parameters": config.get("model_parameters", {}),
        "budget": config.get("budget", {}),
        "eval_timeout_seconds": task.timeout_seconds,
        "eval_command": task.eval_command,
        "environment": current_environment(),
        # Intervention (explicitly NOT gated):
        "backend": config.get("backend"),
        "prompt_addendum": config.get("prompt_addendum", ""),
    }


def assert_fair(spec_a: dict, spec_b: dict) -> str:
    """Raise FairnessError if any control field differs; describe intervention."""
    diverged = [
        field
        for field in CONTROL_FIELDS
        if spec_a.get(field) != spec_b.get(field)
    ]
    if diverged:
        detail = {f: (spec_a.get(f), spec_b.get(f)) for f in diverged}
        raise FairnessError(f"control fields differ: {detail}")
    return (
        f"fair: controls equal; intervention = "
        f"backend {spec_a.get('backend')} vs {spec_b.get('backend')}"
    )
