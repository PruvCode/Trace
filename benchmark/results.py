"""Result storage: append-only JSONL with schema validation + provenance."""

from __future__ import annotations

import json
from pathlib import Path

REQUIRED_RESULT_FIELDS = [
    "task_id",
    "configuration",
    "run",
    "base_commit",
    "success",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "latency_seconds",
    "turns",
    "tool_calls",
]

PROVENANCE_FIELDS = [
    "seed",
    "model",
    "agent",
    "task_version",
    "config_hash",
    "eval_command",
    "exit_code",
    "workspace",
    "prompt_sha256",
    "fixture",
    "error",
    "termination_reason",
    "timed_out",
    "tool_log",
    "git_status",
    "memory_tool_calls",
    "token_source",
    "timestamp",
    "benchmark_version",
]


def validate_result(result: dict) -> None:
    missing = [k for k in REQUIRED_RESULT_FIELDS if k not in result]
    if missing:
        raise ValueError(f"result missing required fields: {missing}")
    if not isinstance(result["success"], bool):
        raise ValueError("result['success'] must be a bool")


def append_result(path: Path, result: dict) -> None:
    validate_result(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(result, sort_keys=True) + "\n")
        fh.flush()


def read_results(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows
