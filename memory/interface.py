"""Memory backend seam: the benchmark speaks only this interface + MCP tools.

A new backend (reference or external) implements these three methods; the
benchmark engine never imports SQLite, Tree-sitter, or backend internals.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class MCPConfig:
    """How the runner reaches a backend's tools over MCP stdio."""

    command: list[str] = field(default_factory=list)
    cwd: str | None = None
    tools: list[str] = field(default_factory=list)


class MemoryBackend(ABC):
    """Lifecycle owned by the runner: setup -> (agent runs) -> teardown."""

    name: str

    @abstractmethod
    def setup(self, workspace: Path) -> MCPConfig:
        """Prepare workspace-local state; return how to reach its MCP tools."""

    @abstractmethod
    def reset(self) -> None:
        """Clear per-run state so the next run starts clean."""

    @abstractmethod
    def teardown(self) -> None:
        """Stop processes, close handles; never leave orphans."""

    def tool_definitions(self) -> list:
        """Agent ToolDefs for this backend's tools ([] when none/not ready).

        Untyped (no agent import) to keep the seam dependency-light; the
        runner combines these with the core file tools.
        """
        return []
