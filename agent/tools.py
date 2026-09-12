"""Core file tools bound to one workspace (Phase 1: read/write/done only).

Every handler resolves the given relative path and rejects escapes outside
the workspace. No shell, no network.
"""

from __future__ import annotations

import json
import time
from hashlib import sha256
from pathlib import Path

from agent.interface import ToolCallRecord, ToolDef


def hash_args(args: dict) -> str:
    """Stable hash of tool args (stored in logs instead of raw values)."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _resolve(workspace: Path, rel: str) -> Path:
    root = workspace.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes workspace: {rel!r}")
    return target


def build_core_tools(workspace: Path) -> list[ToolDef]:
    ws = workspace

    def read_file(path: str) -> str:
        return _resolve(ws, path).read_text(encoding="utf-8")

    def write_file(path: str, content: str) -> dict:
        target = _resolve(ws, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "path": path}

    def done(summary: str) -> dict:
        return {"ok": True, "summary": summary}

    return [
        ToolDef(
            name="read_file",
            description="Read a UTF-8 text file relative to the workspace.",
            json_schema={"path": "string"},
            handler=read_file,
        ),
        ToolDef(
            name="write_file",
            description="Write a UTF-8 text file relative to the workspace.",
            json_schema={"path": "string", "content": "string"},
            handler=write_file,
        ),
        ToolDef(
            name="done",
            description="Signal task completion with a short summary.",
            json_schema={"summary": "string"},
            handler=done,
        ),
    ]


def call_tool(tool: ToolDef, **args) -> tuple[bool, object, ToolCallRecord]:
    """Invoke one tool, capturing latency and a hashed log record."""
    started = time.perf_counter()
    try:
        result = tool.handler(**args)
        ok, error = True, None
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        result = None
        ok, error = False, f"{type(exc).__name__}: {exc}"
    latency_ms = (time.perf_counter() - started) * 1000.0
    record = ToolCallRecord(
        name=tool.name,
        args_hash=hash_args(args),
        latency_ms=latency_ms,
        ok=ok,
        error=error,
    )
    return ok, result, record
