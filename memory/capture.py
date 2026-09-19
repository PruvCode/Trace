"""Capture abstraction for automatic agent session recording.

This module provides a unified interface for capturing coding agent sessions
and persisting them into TRACE memory. Each supported agent implements a
CaptureAdapter that translates the agent's native event stream into
TRACE transcript messages and episodic events.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


@dataclass(frozen=True)
class CapturedMessage:
    """A single message captured from an agent session."""
    role: str              # "user" | "assistant" | "system" | "tool"
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    session_id: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CapturedEvent:
    """An episodic event captured from an agent session (tool use, finding, etc.)."""
    type: str              # "investigation" | "attempt" | "decision" | "observation" | "git_change"
    content: str
    symbol: str | None = None
    file: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    session_id: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SessionInfo:
    """Information about a captured session."""
    session_id: str
    agent: str
    start_time: str
    end_time: str | None = None
    message_count: int = 0
    event_count: int = 0
    project_path: str = ""


class CaptureAdapter(ABC):
    """Abstract base class for agent-specific capture adapters.

    Each adapter translates an agent's native event stream into
    TRACE's unified transcript/event model.
    """

    @property
    @abstractmethod
    def agent_name(self) -> str:
        """Human-readable name of the agent (e.g., 'opencode', 'claude')."""

    @property
    @abstractmethod
    def agent_version(self) -> str:
        """Version of the agent this adapter supports."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the agent is installed and this adapter can work."""

    @abstractmethod
    def capture_session(
        self,
        project_path: Path,
        prompt: str,
        model: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[CapturedMessage | CapturedEvent]:
        """Run a session and yield captured messages/events in real-time.

        Args:
            project_path: The project directory to work in.
            prompt: The initial user prompt/message.
            model: Optional model override.
            session_id: Optional session identifier (generated if not provided).

        Yields:
            CapturedMessage or CapturedEvent objects as they occur.
        """

    @abstractmethod
    def setup_integration(self, project_path: Path) -> dict:
        """Perform one-time setup for automatic capture.

        Returns a dict with setup status and any configuration needed.
        """

    @abstractmethod
    def get_recent_context(
        self,
        project_path: Path,
        query: str,
        limit: int = 10,
    ) -> list[CapturedMessage]:
        """Retrieve relevant historical context for a new session.

        Args:
            project_path: The project directory.
            query: Search query for relevant context.
            limit: Maximum number of messages to return.

        Returns:
            List of relevant captured messages.
        """


@dataclass
class CaptureConfig:
    """Configuration for automatic capture behavior."""
    enabled: bool = True
    capture_transcripts: bool = True
    capture_events: bool = True
    capture_tool_calls: bool = True
    redact_secrets: bool = True
    max_message_length: int = 50000
    session_timeout_seconds: int = 3600