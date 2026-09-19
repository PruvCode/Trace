"""OpenCode execution backend (headless `opencode run`).

Runs the task prompt inside the TRACE workspace with `opencode run --format
json --dir <workspace> --auto`, then maps the JSON event stream onto the
neutral AgentResult contract. Lives alongside LLMAgent; selected via config
`agent: opencode`.

Per-run setup:
- `<workspace>/opencode.json`: fixed permission profile, written by this
  adapter. The benchmark runner (which owns the memory seam) adds the local
  memory-server section for reference runs before this agent executes, and
  this agent removes the file afterwards. The raw event stream is kept at
  `<workspace>/.agent-memory/opencode_transcript.jsonl`.

Metric mapping (documented, no estimation):
- turns = step_start events; tool_calls = tool_use parts; usage = summed
  per-step provider tokens; memory calls = tool names mapped back from the
  `trace_memory_` prefix to canonical TRACE memory tool names so the
  runner's memory_tool_calls accounting keeps working.
- No done-tool exists here: clean process exit after a final stop step
  means completed. Budget step caps are NOT enforceable inside
  `opencode run`; wall-clock timeout still applies.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from shutil import which

from agent.interface import AgentResult, Budget, CodingAgent, ToolCallRecord
from agent.tools import hash_args

MEMORY_SERVER_NAME = "trace_memory"
MEMORY_TOOL_PREFIX = MEMORY_SERVER_NAME + "_"

MEMORY_TOOL_NAMES = frozenset(
    {
        "find_definition",
        "find_callers",
        "search_symbols",
        "record_event",
        "search_events",
        "get_git_history",
    }
)

# File-navigation basics allowed (read/edit/glob/grep); everything else the
# TRACE core tools do not provide stays denied (no shell, no subagents, no
# network). Identical for baseline and reference runs.
PERMISSION_PROFILE = {
    "read": "allow",
    "edit": "allow",
    "glob": "allow",
    "grep": "allow",
    "bash": "deny",
    "task": "deny",
    "webfetch": "deny",
    "websearch": "deny",
}

CONFIG_FILENAME = "opencode.json"
TRANSCRIPT_FILENAME = "opencode_transcript.jsonl"


def canonical_tool_name(reported: str) -> str:
    """Map OpenCode tool names back to TRACE names (server prefix stripped)."""
    if reported.startswith(MEMORY_TOOL_PREFIX):
        short = reported[len(MEMORY_TOOL_PREFIX):]
        if short in MEMORY_TOOL_NAMES:
            return short
    return reported


def build_opencode_config() -> dict:
    """Permission-only per-run opencode.json content (no secrets)."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": dict(PERMISSION_PROFILE),
    }


def parse_events(lines: list[str]) -> dict:
    """Fold an `opencode run --format json` stream into run metrics."""
    turns = 0
    calls = 0
    in_tok = 0
    out_tok = 0
    log: list[ToolCallRecord] = []
    skipped = 0
    last_reason: str | None = None
    saw_usage = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(event, dict):
            skipped += 1
            continue
        etype = event.get("type")
        if etype == "step_start":
            turns += 1
        elif etype == "tool_use":
            calls += 1
            part = event.get("part", {}) if isinstance(event.get("part"), dict) else {}
            reported = str(part.get("tool", "unknown"))
            state = part.get("state", {}) if isinstance(part.get("state"), dict) else {}
            status = state.get("status", "unknown")
            ok = status == "completed"
            timing = state.get("time", {}) if isinstance(state.get("time"), dict) else {}
            try:
                latency_ms = float(timing.get("end", 0)) - float(timing.get("start", 0))
            except (TypeError, ValueError):
                latency_ms = 0.0
            inputs = state.get("input")
            args = inputs if isinstance(inputs, dict) else {}
            log.append(
                ToolCallRecord(
                    name=canonical_tool_name(reported),
                    args_hash=hash_args(args),
                    latency_ms=max(0.0, latency_ms),
                    ok=ok,
                    error=None if ok else f"opencode_tool_{status}",
                )
            )
        elif etype == "step_finish":
            part = event.get("part", {}) if isinstance(event.get("part"), dict) else {}
            if part.get("reason"):
                last_reason = str(part["reason"])
            tokens = part.get("tokens", {}) if isinstance(part.get("tokens"), dict) else {}
            if isinstance(tokens.get("input"), int) and isinstance(tokens.get("output"), int):
                in_tok += tokens["input"]
                out_tok += tokens["output"]
                saw_usage = True
    return {
        "turns": turns,
        "tool_calls": calls,
        "input_tokens": in_tok if saw_usage else None,
        "output_tokens": out_tok if saw_usage else None,
        "tool_log": log,
        "skipped_lines": skipped,
        "last_reason": last_reason,
    }


class OpenCodeAgent(CodingAgent):
    """Headless OpenCode backend. No network/auth handled here (free tier)."""

    def __init__(self, model: str, opencode_bin: str = "opencode") -> None:
        if not model:
            raise ValueError("OpenCodeAgent requires a model (provider/model)")
        self._model = model
        self._bin = opencode_bin

    def _resolve_binary(self) -> str:
        """Resolve the launcher (bare names are not executable on Windows)."""
        if Path(self._bin).is_file():
            return self._bin
        for candidate in (self._bin, "opencode.exe", "opencode.cmd"):
            found = which(candidate)
            if found:
                return found
        raise FileNotFoundError(
            f"opencode_not_installed: cannot resolve {self._bin!r} on PATH"
        )

    def run(
        self,
        workspace: Path,
        prompt: str,
        tools: list,
        budget: Budget,
    ) -> AgentResult:
        _ = tools  # tool surface is fixed by the workspace config, not ToolDefs
        ws = workspace.resolve()
        config_path = ws / CONFIG_FILENAME
        transcript_path = ws / ".agent-memory" / TRANSCRIPT_FILENAME
        if not config_path.exists():
            config_path.write_text(
                json.dumps(build_opencode_config(), indent=2), encoding="utf-8"
            )
        # Flags MUST precede the message: `run [message..]` swallows trailing
        # flags into the message array (silently dropping --format/--auto/-m).
        cmd = [
            self._resolve_binary(),
            "run",
            "--format",
            "json",
            "-m",
            self._model,
            "--dir",
            str(ws),
            "--auto",
            prompt,
        ]
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(ws),
                capture_output=True,
                timeout=budget.timeout_seconds,
            )
            stdout = completed.stdout.decode("utf-8", errors="replace")
            stderr = completed.stderr.decode("utf-8", errors="replace")
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            raw = exc.stdout if isinstance(exc.stdout, bytes) else b""
            self._write_transcript(
                transcript_path, raw.decode("utf-8", errors="replace")
            )
            return AgentResult(
                status="timeout",
                termination_reason=f"opencode exceeded timeout_seconds={budget.timeout_seconds}",
                turns=0,
                tool_calls=0,
                tool_log=[],
                error="opencode timeout",
            )
        except (OSError, ValueError) as exc:
            return AgentResult(
                status="error",
                termination_reason="opencode_launch_error",
                turns=0,
                tool_calls=0,
                tool_log=[],
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            try:
                config_path.unlink(missing_ok=True)
            except OSError:
                pass
        parsed = parse_events(stdout.splitlines())
        self._write_transcript(transcript_path, stdout)
        if returncode != 0:
            return AgentResult(
                status="error",
                termination_reason="opencode_execution_error",
                turns=parsed["turns"],
                tool_calls=parsed["tool_calls"],
                tool_log=parsed["tool_log"],
                input_tokens=parsed["input_tokens"],
                output_tokens=parsed["output_tokens"],
                error=(stderr or f"exit {returncode}").strip()[-2000:],
            )
        return AgentResult(
            status="completed",
            termination_reason=f"opencode_stopped:{parsed['last_reason'] or 'unknown'}",
            turns=parsed["turns"],
            tool_calls=parsed["tool_calls"],
            tool_log=parsed["tool_log"],
            input_tokens=parsed["input_tokens"],
            output_tokens=parsed["output_tokens"],
        )

    @staticmethod
    def _write_transcript(path: Path, content: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError:
            pass
