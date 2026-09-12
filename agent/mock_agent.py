"""Deterministic scripted agent for harness testing (no LLM, no network).

Behaviors (selected via config `mock.behavior`, overridable by CLI):
- "pass":  read session.py, apply the one-line fix, done  -> evaluator PASSES
- "fail":  read session.py, write a wrong value, done      -> evaluator FAILS
- "error": raise immediately                               -> harness error path
- "slow":  sleep past the budget without writing           -> runner timeout path
- "memory": use available memory tools, then fix, done     -> memory plumbing

One tool call = one turn. Token usage is None (mock performs no model calls).
"""

from __future__ import annotations

import time
from pathlib import Path

from agent.interface import AgentResult, Budget, CodingAgent, ToolDef
from agent.tools import call_tool

TARGET_FILE = "auth/session.py"


class MockAgent(CodingAgent):
    def __init__(self, behavior: str = "pass", sleep_seconds: float = 30.0) -> None:
        if behavior not in ("pass", "fail", "error", "slow", "memory"):
            raise ValueError(f"unknown mock behavior: {behavior!r}")
        self.behavior = behavior
        self.sleep_seconds = sleep_seconds

    @staticmethod
    def _fixed(content: str) -> str:
        buggy_line = "return 15  # BUG: should return SESSION_TIMEOUT_MINUTES"
        if buggy_line in content:
            return content.replace(buggy_line, "return SESSION_TIMEOUT_MINUTES")
        if "return 15" in content:
            return content.replace("return 15", "return SESSION_TIMEOUT_MINUTES")
        return content

    def run(
        self,
        workspace: Path,
        prompt: str,
        tools: list[ToolDef],
        budget: Budget,
    ) -> AgentResult:
        _ = prompt  # prompt is recorded by the runner; mock follows its script
        if self.behavior == "error":
            raise RuntimeError("mock agent crash (behavior=error)")
        if self.behavior == "slow":
            time.sleep(self.sleep_seconds)
            return AgentResult(
                status="completed",
                termination_reason="slow_behavior_finished_without_writing",
                turns=0,
                tool_calls=0,
                tool_log=[],
            )
        by_name = {t.name: t for t in tools}
        log: list = []
        turns = 0
        calls = 0

        def step(name: str, **args):
            nonlocal turns, calls
            turns += 1
            calls += 1
            if turns > budget.max_turns or calls > budget.max_tool_calls:
                return (
                    False,
                    None,
                    AgentResult(
                        status="budget_exhausted",
                        termination_reason=f"exceeded budget at tool {name}",
                        turns=turns,
                        tool_calls=calls,
                        tool_log=list(log),
                    ),
                )
            ok, result, record = call_tool(by_name[name], **args)
            log.append(record)
            return ok, result, None

        ok, content, stop = step("read_file", path=TARGET_FILE)
        if stop is not None:
            return stop
        if not ok or not isinstance(content, str):
            return AgentResult(
                status="error",
                termination_reason="read_failed",
                turns=turns,
                tool_calls=calls,
                tool_log=log,
                error="mock could not read target file",
            )

        if self.behavior == "pass":
            new_content = self._fixed(content)
        elif self.behavior == "memory":
            # Exercise whichever memory tools the run provides, then fix.
            # Works with or without memory tools (tolerant by design).
            if "search_symbols" in by_name:
                _ok, _res, stop = step("search_symbols", query="timeout")
                if stop is not None:
                    return stop
            if "record_event" in by_name:
                _ok, _res, stop = step(
                    "record_event",
                    type="observation",
                    repo="benchmark-workspace",
                    symbol="get_session_timeout",
                    payload={"finding": "timeout value looks wrong"},
                )
                if stop is not None:
                    return stop
            new_content = self._fixed(content)
        else:  # "fail": a confident-looking but wrong edit
            new_content = content.replace(
                "return 15  # BUG: should return SESSION_TIMEOUT_MINUTES",
                "return 999  # WRONG",
            ).replace("return 15", "return 999").replace(
                "return SESSION_TIMEOUT_MINUTES", "return 999"
            )

        ok, _result, stop = step(
            "write_file", path=TARGET_FILE, content=new_content
        )
        if stop is not None:
            return stop
        _ok, _done, stop = step("done", summary=f"mock {self.behavior} finished")
        if stop is not None:
            return stop
        return AgentResult(
            status="completed",
            termination_reason="done_tool_called",
            turns=turns,
            tool_calls=calls,
            tool_log=log,
        )
