"""Reference memory backend: per-run local MCP server (Phase 4.2).

Lifecycle per benchmark run: index the isolated workspace structurally into
<workspace>/.agent-memory/memory.db, apply only the config's explicit
preseed events, spawn one MCP server child, expose its tools. Teardown
kills the child; reset additionally removes the database file. No shared
state between runs by construction (fresh workspace + fresh database).
"""

from __future__ import annotations

import sys
from pathlib import Path

from benchmark import mcp_bridge as bridge_mod
from memory import episodic as episodic_mod
from memory import store as store_mod
from memory import structural as structural_mod
from memory.interface import MCPConfig, MemoryBackend

MEMORY_DIRNAME = ".agent-memory"
DB_FILENAME = "memory.db"


def workspace_db_path(workspace: Path) -> Path:
    return workspace.resolve() / MEMORY_DIRNAME / DB_FILENAME


class ReferenceBackend(MemoryBackend):
    name = "reference"

    def __init__(self, repo_root: Path, preseed: list[dict] | tuple = ()) -> None:
        self._repo_root = Path(repo_root)
        self._preseed = list(preseed or [])
        self._bridge: bridge_mod.MCPBridgeSession | None = None
        self._db: Path | None = None
        self._tools: list = []

    def setup(self, workspace: Path) -> MCPConfig:
        ws = workspace.resolve()
        db = workspace_db_path(ws)
        structural_mod.index_workspace(ws, db)
        if self._preseed:
            if not isinstance(self._preseed, list) or not all(
                isinstance(e, dict) for e in self._preseed
            ):
                raise ValueError("preseed must be a list of event dicts")
            conn = store_mod.connect(db)
            try:
                for entry in self._preseed:
                    try:
                        episodic_mod.record_event(conn, **entry)
                    except TypeError as exc:
                        raise ValueError(
                            f"invalid preseed event {entry!r}: {exc}"
                        ) from exc
            finally:
                conn.close()
        command = [
            sys.executable,
            "-m",
            "memory.mcp_server",
            "--db",
            str(db),
            "--repo",
            str(ws),
        ]
        bridge = bridge_mod.MCPBridgeSession(command, cwd=str(self._repo_root))
        bridge.start()  # raises MCPBridgeError -> runner records memory_setup_failed
        self._bridge = bridge
        self._db = db
        self._tools = bridge.tool_definitions()
        return MCPConfig(
            command=command,
            cwd=str(self._repo_root),
            tools=[t.name for t in self._tools],
        )

    def tool_definitions(self) -> list:
        """Agent ToolDefs for this run's memory tools ([] before setup)."""
        return list(self._tools)

    def reset(self) -> None:
        self.teardown()
        if self._db is not None and self._db.exists():
            try:
                self._db.unlink()
            except OSError:
                pass
        self._db = None
        self._tools = []

    def teardown(self) -> None:
        """Best-effort shutdown; never raises so run outcomes are not masked."""
        bridge, self._bridge = self._bridge, None
        self._tools = []
        if bridge is not None:
            try:
                bridge.close()
            except Exception:  # noqa: BLE001 - teardown never masks results
                pass
