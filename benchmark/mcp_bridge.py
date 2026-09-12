"""MCP stdio bridge: server tools -> agent ToolDefs (runner-owned transport).

Lives in benchmark/ (not agent/) because the architecture guards keep MCP
implementation details out of the agent layer. Owns exactly one server child
process per session: start() spawns + initializes (with bounded retries),
close() shuts everything down. Transport death fails closed per call.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.interface import ToolDef


class MCPBridgeError(RuntimeError):
    """MCP transport failure: startup, call, or shutdown. Never hidden."""


def unwrap_result(result: Any) -> Any:
    """Best-effort parse of a CallToolResult into plain data."""
    for attr in ("structured_content", "structuredContent"):
        structured = getattr(result, attr, None)
        if isinstance(structured, dict):
            if set(structured) == {"result"}:
                return structured["result"]
            return structured
        if structured is not None:
            return structured
    texts = [
        getattr(block, "text", None) for block in getattr(result, "content", [])
    ]
    texts = [t for t in texts if isinstance(t, str)]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except (TypeError, ValueError):
            return texts[0]
    return texts


class MCPBridgeSession:
    """One server child + one client session, driven on a private loop thread."""

    def __init__(
        self,
        command: list[str],
        cwd: str | None = None,
        startup_timeout: float = 30.0,
        call_timeout: float = 60.0,
        startup_retries: int = 3,
    ) -> None:
        if not command:
            raise MCPBridgeError("empty MCP command")
        self._command = list(command)
        self._cwd = cwd
        self._startup_timeout = startup_timeout
        self._call_timeout = call_timeout
        self._startup_retries = max(1, startup_retries)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stdio_ctx = None
        self._session_ctx: ClientSession | None = None
        self._session: ClientSession | None = None
        self._tool_names: list[str] = []
        self._closed = False

    def _await(self, coro, timeout: float):
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(
            timeout=timeout
        )

    async def _open(self) -> list[str]:
        params = StdioServerParameters(
            command=self._command[0], args=self._command[1:], cwd=self._cwd
        )
        self._stdio_ctx = stdio_client(params)
        read, write = await self._stdio_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()
        listed = await self._session.list_tools()
        return [t.name for t in listed.tools]

    async def _close_contexts(self) -> None:
        for ctx in (self._session_ctx, self._stdio_ctx):
            if ctx is None:
                continue
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                pass
        self._session_ctx = None
        self._stdio_ctx = None
        self._session = None

    def start(self) -> None:
        """Spawn the server and initialize. Raises MCPBridgeError on failure."""
        if self._thread is not None:
            raise MCPBridgeError("bridge already started")
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True
        )
        self._thread.start()
        last_error: Exception | None = None
        for _ in range(self._startup_retries):
            try:
                self._tool_names = self._await(self._open(), self._startup_timeout)
                return
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                last_error = exc
                try:
                    self._await(self._close_contexts(), 10.0)
                except Exception:  # noqa: BLE001 - cleanup best-effort
                    pass
                time.sleep(2.0)
        self.close()
        raise MCPBridgeError(f"mcp startup failed: {last_error}")

    def tool_names(self) -> list[str]:
        return list(self._tool_names)

    def tool_definitions(self) -> list[ToolDef]:
        defs = []
        for name in self._tool_names:
            defs.append(
                ToolDef(
                    name=name,
                    description=f"Project memory tool: {name}.",
                    json_schema={"type": "object"},
                    handler=self._make_handler(name),
                )
            )
        return defs

    def _make_handler(self, name: str):
        def handler(**kwargs):
            return self.call(name, kwargs)

        return handler

    def call(self, name: str, args: dict | None = None) -> Any:
        """Call one tool. Transport death or tool error raises MCPBridgeError."""
        if self._session is None:
            raise MCPBridgeError("bridge is not started")
        try:
            result = self._await(
                self._session.call_tool(name, args or {}), self._call_timeout
            )
        except Exception as exc:  # noqa: BLE001 - fail closed per call
            raise MCPBridgeError(f"mcp tool call failed: {name}: {exc}") from exc
        if bool(getattr(result, "is_error", False)):
            raise MCPBridgeError(f"mcp tool error: {name}: {unwrap_result(result)}")
        return unwrap_result(result)

    def alive(self) -> bool:
        """Best-effort liveness probe (used by tests, not the hot path)."""
        if self._session is None or self._loop is None:
            return False
        try:
            self._await(self._session.list_tools(), 5.0)
            return True
        except Exception:  # noqa: BLE001 - any failure means not alive
            return False

    def close(self) -> None:
        """Idempotent shutdown: contexts, loop, thread. Never raises."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._loop is not None:
                try:
                    self._await(self._close_contexts(), 10.0)
                except Exception:  # noqa: BLE001 - shutdown best-effort
                    pass
                self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:  # noqa: BLE001 - shutdown best-effort
            pass
        if self._thread is not None:
            self._thread.join(timeout=10.0)
        self._loop = None
        self._thread = None
        self._session = None
