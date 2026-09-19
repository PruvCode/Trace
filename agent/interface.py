"""CodingAgent contract (Phase 1: mock only, one real provider later).

run(workspace, prompt, tools, budget) -> AgentResult. See the approved plan
section 4 for the full normative contract. No vendor types here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ToolCallRecord:
    name: str
    args_hash: str
    latency_ms: float
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    json_schema: dict
    handler: Callable[..., Any]


@dataclass(frozen=True)
class Budget:
    max_turns: int = 10
    max_tool_calls: int = 20
    timeout_seconds: float = 300.0


@dataclass(frozen=True)
class AgentResult:
    status: str  # "completed" | "budget_exhausted" | "timeout" | "error"
    termination_reason: str
    turns: int
    tool_calls: int
    tool_log: list = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    transcript_path: str | None = None
    error: str | None = None


class CodingAgent(ABC):
    """Provider-neutral agent. The runner is the sole caller of run()."""

    @abstractmethod
    def run(
        self,
        workspace: Path,
        prompt: str,
        tools: list[ToolDef],
        budget: Budget,
    ) -> AgentResult:
        """Execute the task in workspace; leave code changes in place."""
