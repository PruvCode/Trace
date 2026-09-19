"""Baseline backend: no memory capability, zero tools.

Used by the `baseline` config so comparisons differ from memory configs only
in the memory intervention (see benchmark/fairness.py).
"""

from __future__ import annotations

from pathlib import Path

from memory.interface import MCPConfig, MemoryBackend


class NullBackend(MemoryBackend):
    name = "null"

    def setup(self, workspace: Path) -> MCPConfig:
        _ = workspace
        return MCPConfig(command=[], cwd=None, tools=[])

    def reset(self) -> None:
        return None

    def teardown(self) -> None:
        return None
