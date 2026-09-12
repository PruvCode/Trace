"""Metrics helpers: wall-clock timing only (tokens come from the agent layer)."""

from __future__ import annotations

import time


class RunTimer:
    def __init__(self) -> None:
        self._start = time.perf_counter()

    def elapsed(self) -> float:
        return time.perf_counter() - self._start
