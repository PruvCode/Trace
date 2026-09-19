"""OpenCode capture adapter for automatic session recording.

Captures OpenCode sessions by running `opencode run --format json` and
parsing the JSONL event stream in real-time, persisting messages and
events into TRACE memory.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from shutil import which

from memory.capture import (
    CaptureAdapter,
    CaptureConfig,
    CapturedEvent,
    CapturedMessage,
    SessionInfo,
)
from memory import transcript as transcript_mod
from memory import episodic as episodic_mod
from memory import store as store_mod
from trace_memory import project as project_mod


MEMORY_TOOL_NAMES = frozenset({
    "find_definition",
    "find_callers",
    "search_symbols",
    "record_event",
    "search_events",
    "get_git_history",
    "record_transcript_message",
    "search_transcripts",
    "get_transcript_session",
})


class OpenCodeCaptureAdapter(CaptureAdapter):
    """Capture adapter for OpenCode agent."""

    @property
    def agent_name(self) -> str:
        return "opencode"

    @property
    def agent_version(self) -> str:
        return ">=1.0.0"

    def is_available(self) -> bool:
        return which("opencode") is not None

    def _resolve_binary(self) -> str:
        for candidate in ("opencode", "opencode.exe", "opencode.cmd"):
            found = which(candidate)
            if found:
                return found
        raise FileNotFoundError("opencode not found on PATH")

    def _sanitize_content(self, content: str, config: CaptureConfig) -> str:
        """Redact sensitive information from content if enabled."""
        if not config.redact_secrets:
            return content
        # Basic redaction patterns for common secrets
        import re
        patterns = [
            (r'(api[_-]?key|secret|password|token|credential)\s*[:=]\s*\S+', r'\1=***REDACTED***'),
            (r'(sk|pk)_[a-zA-Z0-9]{20,}', '***REDACTED***'),
            (r'gh[psuo]_[a-zA-Z0-9]{36}', '***REDACTED***'),
            (r'xox[baprs]-[\w-]{10,}', '***REDACTED***'),
        ]
        result = content
        for pattern, replacement in patterns:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
        return result

    def _truncate(self, content: str, max_length: int) -> str:
        if len(content) <= max_length:
            return content
        return content[:max_length] + "...[truncated]"

    def _extract_tool_calls(self, event: dict) -> list[dict]:
        """Extract tool calls from OpenCode event."""
        calls = []
        part = event.get("part")
        if not isinstance(part, dict):
            return calls
        tool = part.get("tool")
        state = part.get("state", {})
        if tool and isinstance(state, dict):
            calls.append({
                "tool": tool,
                "input": state.get("input"),
                "status": state.get("status"),
                "output": state.get("output"),
            })
        return calls

    def _map_tool_to_event(self, tool_call: dict, session_id: str) -> CapturedEvent | None:
        """Map a tool call to an episodic event if it's a memory tool."""
        tool_name = tool_call.get("tool", "")
        # Strip trace_memory_ prefix if present
        if tool_name.startswith("trace_memory_"):
            tool_name = tool_name[len("trace_memory_"):]
        if tool_name in MEMORY_TOOL_NAMES:
            input_data = tool_call.get("input", {})
            output_data = tool_call.get("output", {})
            content = f"Memory tool: {tool_name}"
            if input_data:
                content += f" | input: {json.dumps(input_data)}"
            if output_data:
                content += f" | output: {json.dumps(output_data)[:500]}"
            return CapturedEvent(
                type="observation",
                content=content,
                symbol=input_data.get("symbol") if isinstance(input_data, dict) else None,
                file=input_data.get("file") if isinstance(input_data, dict) else None,
                session_id=session_id,
                metadata={"tool": tool_name, "input": input_data, "output": output_data},
            )
        return None

    def capture_session(
        self,
        project_path: Path,
        prompt: str,
        model: str | None = None,
        session_id: str | None = None,
        config: CaptureConfig | None = None,
    ):
        """Run OpenCode with JSON output and yield captured messages/events."""
        if config is None:
            config = CaptureConfig()

        session_id = session_id or str(uuid.uuid4())[:8]
        project_path = project_path.resolve()

        # Ensure TRACE is initialized for this project
        if not project_mod.is_initialized(project_path):
            project_mod.init_project(project_path)

        db_path = project_mod.db_path(project_path)

        cmd = [
            self._resolve_binary(),
            "run",
            "--format",
            "json",
            "--dir",
            str(project_path),
            "--auto",
        ]
        if model:
            cmd.extend(["-m", model])
        cmd.append(prompt)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(project_path),
        )

        try:
            # Yield session start
            yield CapturedMessage(
                role="system",
                content=f"Session started: {prompt[:200]}",
                session_id=session_id,
                metadata={"agent": "opencode", "prompt": prompt},
            )

            # Process stdout line by line
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")

                if etype == "step_start":
                    # Assistant message starting
                    pass

                elif etype == "tool_use":
                    # Tool call - could be memory tool or other
                    tool_calls = self._extract_tool_calls(event)
                    for tc in tool_calls:
                        mapped = self._map_tool_to_event(tc, session_id)
                        if mapped and config.capture_events:
                            conn = store_mod.connect(db_path)
                            try:
                                event_id = episodic_mod.record_event(
                                    conn,
                                    type=mapped.type,
                                    source="agent",
                                    timestamp=mapped.timestamp,
                                    repo=project_path.name,
                                    symbol=mapped.symbol,
                                    file=mapped.file,
                                    payload=mapped.metadata,
                                )
                                yield CapturedEvent(
                                    type=mapped.type,
                                    content=mapped.content,
                                    symbol=mapped.symbol,
                                    file=mapped.file,
                                    timestamp=mapped.timestamp,
                                    session_id=session_id,
                                    metadata={**mapped.metadata, "event_id": event_id},
                                )
                            finally:
                                conn.close()

                elif etype == "step_finish":
                    # Assistant message complete
                    part = event.get("part", {})
                    reason = part.get("reason", "") if isinstance(part, dict) else ""
                    tokens = part.get("tokens", {}) if isinstance(part, dict) else {}

                    content_parts = []
                    if reason:
                        content_parts.append(reason)

                    if content_parts:
                        content = " ".join(content_parts)
                        content = self._sanitize_content(content, config)
                        content = self._truncate(content, config.max_message_length)

                        msg = CapturedMessage(
                            role="assistant",
                            content=content,
                            session_id=session_id,
                            metadata={
                                "tokens_input": tokens.get("input"),
                                "tokens_output": tokens.get("output"),
                            },
                        )

                        if config.capture_transcripts:
                            conn = store_mod.connect(db_path)
                            try:
                                transcript_mod.record_message(
                                    conn,
                                    session_id=session_id,
                                    role="user" if not content_parts else "assistant",
                                    content=prompt if not content_parts else content,
                                    timestamp=msg.timestamp,
                                    metadata=msg.metadata,
                                )
                            finally:
                                conn.close()
                        yield msg

                elif etype == "user_message":
                    # User message (if OpenCode emits this)
                    content = event.get("message", "")
                    if content:
                        content = self._sanitize_content(content, config)
                        content = self._truncate(content, config.max_message_length)
                        yield CapturedMessage(
                            role="user",
                            content=content,
                            session_id=session_id,
                            metadata={},
                        )

        except Exception as e:
            yield CapturedMessage(
                role="system",
                content=f"Capture error: {e}",
                session_id=session_id,
                metadata={"error": str(e)},
            )
        finally:
            proc.wait(timeout=5)
            yield CapturedMessage(
                role="system",
                content="Session ended",
                session_id=session_id,
                metadata={"exit_code": proc.returncode},
            )

    def setup_integration(self, project_path: Path) -> dict:
        """Set up OpenCode integration for a project."""
        project_path = project_path.resolve()

        # Ensure TRACE is initialized
        if not project_mod.is_initialized(project_path):
            project_mod.init_project(project_path)

        # Check if OpenCode config exists
        config_path = project_path / "opencode.json"
        if config_path.exists():
            return {
                "status": "already_configured",
                "config_path": str(config_path),
                "message": "OpenCode config already exists",
            }

        # Create basic OpenCode config with MCP memory server
        # OpenCode expects MCP config with "type": "local" and "enabled": true
        mcp_command = project_mod.mcp_server_command(project_path)
        opencode_config = {
            "$schema": "https://opencode.ai/config.json",
            "permission": {
                "read": "allow",
                "edit": "allow",
                "glob": "allow",
                "grep": "allow",
                "bash": "deny",
                "task": "deny",
                "webfetch": "deny",
                "websearch": "deny",
            },
            "mcp": {
                "trace-memory": {
                    "type": "local",
                    "enabled": True,
                    "command": mcp_command,
                }
            },
        }

        config_path.write_text(json.dumps(opencode_config, indent=2), encoding="utf-8")

        return {
            "status": "created",
            "config_path": str(config_path),
            "message": "Created opencode.json with TRACE MCP server",
        }

    def get_recent_context(
        self,
        project_path: Path,
        query: str,
        limit: int = 10,
    ) -> list[CapturedMessage]:
        """Retrieve relevant historical context from TRACE transcript memory."""
        project_path = project_path.resolve()
        if not project_mod.is_initialized(project_path):
            return []

        db_path = project_mod.db_path(project_path)
        conn = store_mod.connect(db_path)
        try:
            results = transcript_mod.search_messages(
                conn,
                query=query,
                limit=limit,
            )
            return [
                CapturedMessage(
                    role=r.role,
                    content=r.content,
                    timestamp=r.timestamp,
                    session_id=r.session_id,
                    metadata=r.metadata,
                )
                for r in results
            ]
        finally:
            conn.close()


def create_opencode_adapter() -> OpenCodeCaptureAdapter:
    """Factory function to create OpenCode adapter."""
    return OpenCodeCaptureAdapter()