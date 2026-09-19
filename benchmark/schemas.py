"""Shared schemas: task definition and run result (no orchestration here)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TaskConfig:
    task_id: str
    category: str
    fixture: str  # repo-relative fixture directory, e.g. "fixtures/toy_repo"
    base_commit: str  # pinned git SHA the workspace must start from
    prompt_file: str
    eval_command: list[str]
    timeout_seconds: int
    tags: list[str]
    task_dir: str  # repo-relative task directory
    task_version: str  # sha256 over task definition files
    prompt_sha256: str  # sha256 of prompt bytes (fairness: base prompt equality)
    prompt: str  # exact prompt text handed to the agent
    preseed: list[dict] = field(default_factory=list)  # optional episodic events


@dataclass(frozen=True)
class EvalResult:
    success: bool
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
