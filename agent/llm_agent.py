"""Provider-neutral real-agent layer (Phase 4.1).

Neutral message protocol owned here (no vendor types cross this boundary):
- {"role": "system" | "user", "content": str}
- {"role": "assistant", "content": str, "tool_calls": [{"id","name","args"}]}
- {"role": "tool", "tool_call_id": str, "name": str, "content": str}

LLMClient.complete(messages, tools) -> LLMResponse. Each client translates
both directions and reports exact provider usage or None. Token counts are
never estimated: unknown usage stays None end to end.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import openai

from agent.interface import AgentResult, Budget, CodingAgent, ToolCallRecord, ToolDef
from agent.tools import call_tool, hash_args


@dataclass(frozen=True)
class LLMToolCall:
    id: str
    name: str
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class LLMResponse:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    usage: LLMUsage | None = None


class LLMClient(ABC):
    """Provider-neutral next-action source. Implementations own translation."""

    @abstractmethod
    def complete(self, messages: list[dict], tools: list[ToolDef]) -> LLMResponse:
        """Return the model's reply. May raise LLMAuthError/LLMProviderError."""


class LLMAuthError(RuntimeError):
    """Credentials missing or rejected. Never retried, never hidden."""


class LLMProviderError(RuntimeError):
    """Provider/network/protocol failure, or unparseable provider output."""


def _result_to_text(result: Any, max_chars: int = 12000) -> str:
    try:
        text = json.dumps(result, default=str)
    except (TypeError, ValueError):
        text = str(result)
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncated]"
    return text


class LLMAgent(CodingAgent):
    """Tool-loop agent: ask client, execute tools, feed results back."""

    def __init__(self, client: LLMClient, model: str, max_result_chars: int = 12000) -> None:
        self._client = client
        self._model = model
        self._max_result_chars = max_result_chars

    def _exhausted(
        self, reason: str, turns: int, calls: int, log: list,
        in_tok: int | None, out_tok: int | None,
    ) -> AgentResult:
        return AgentResult(
            status="budget_exhausted",
            termination_reason=reason,
            turns=turns,
            tool_calls=calls,
            tool_log=list(log),
            input_tokens=in_tok,
            output_tokens=out_tok,
        )

    def run(
        self,
        workspace: Path,
        prompt: str,
        tools: list[ToolDef],
        budget: Budget,
    ) -> AgentResult:
        _ = workspace  # tools are pre-bound to the workspace by the runner
        by_name = {t.name: t for t in tools}
        messages = [{"role": "system", "content": prompt}]
        log: list = []
        turns = 0
        calls = 0
        in_tok: int | None = None
        out_tok: int | None = None

        while True:
            turns += 1
            if turns > budget.max_turns:
                return self._exhausted(
                    f"exceeded max_turns={budget.max_turns}",
                    turns, calls, log, in_tok, out_tok,
                )
            # Client errors propagate: the runner maps them to the failure
            # taxonomy (llm_auth_missing / llm_provider_error).
            response = self._client.complete(list(messages), tools)
            if response.usage is not None:
                in_tok = (in_tok or 0) + response.usage.input_tokens
                out_tok = (out_tok or 0) + response.usage.output_tokens
            messages.append(
                {
                    "role": "assistant",
                    "content": response.text,
                    "tool_calls": [asdict(tc) for tc in response.tool_calls],
                }
            )
            if not response.tool_calls:
                return AgentResult(
                    status="completed",
                    termination_reason="stopped_without_tool_calls",
                    turns=turns,
                    tool_calls=calls,
                    tool_log=log,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                )
            finished = False
            for tc in response.tool_calls:
                calls += 1
                if calls > budget.max_tool_calls:
                    return self._exhausted(
                        f"exceeded max_tool_calls={budget.max_tool_calls}",
                        turns, calls, log, in_tok, out_tok,
                    )
                tool = by_name.get(tc.name)
                if tool is None:
                    record = ToolCallRecord(
                        name=tc.name,
                        args_hash=hash_args(tc.args),
                        latency_ms=0.0,
                        ok=False,
                        error=f"unknown tool: {tc.name}",
                    )
                    log.append(record)
                    result_text = f"error: unknown tool: {tc.name}"
                else:
                    ok, result, record = call_tool(tool, **tc.args)
                    log.append(record)
                    result_text = _result_to_text(result, self._max_result_chars)
                    if tc.name == "done" and ok:
                        finished = True
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": result_text,
                    }
                )
            if finished:
                return AgentResult(
                    status="completed",
                    termination_reason="done_tool_called",
                    turns=turns,
                    tool_calls=calls,
                    tool_log=log,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                )


class OpenAICompatibleClient(LLMClient):
    """OpenAI-compatible HTTP client (works with any compatible base_url).

    Translates the neutral protocol both directions. Only used for real
    (manual, credentialed) runs — never in the automated test suite.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        request_timeout: float = 60.0,
    ) -> None:
        key = (
            api_key
            or os.environ.get("TRACE_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        if not key:
            raise LLMAuthError(
                "no API key: set TRACE_API_KEY or OPENAI_API_KEY"
            )
        self._model = model
        self._client = openai.OpenAI(
            api_key=key, base_url=base_url, timeout=request_timeout, max_retries=2
        )

    @staticmethod
    def _to_spec(tool: ToolDef) -> dict:
        parameters = tool.json_schema or {}
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            parameters = {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": parameters,
            },
        }

    @staticmethod
    def _to_provider_message(message: dict) -> dict:
        role = message["role"]
        if role in ("system", "user"):
            return {"role": role, "content": message.get("content", "")}
        if role == "assistant":
            outgoing: dict = {"role": "assistant", "content": message.get("content") or None}
            calls = message.get("tool_calls", [])
            if calls:
                outgoing["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("args", {})),
                        },
                    }
                    for tc in calls
                ]
            return outgoing
        if role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message["tool_call_id"],
                "content": message.get("content", ""),
            }
        raise LLMProviderError(f"unsupported message role: {role!r}")

    def complete(self, messages: list[dict], tools: list[ToolDef]) -> LLMResponse:
        try:
            provider_messages = [self._to_provider_message(m) for m in messages]
        except LLMProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - malformed history fails closed
            raise LLMProviderError(f"cannot translate messages: {exc}") from exc
        specs = [self._to_spec(t) for t in tools]
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=provider_messages,
                tools=specs if specs else None,
                tool_choice="auto" if specs else "none",
            )
        except openai.AuthenticationError as exc:
            raise LLMAuthError(f"provider rejected credentials: {exc}") from exc
        except openai.OpenAIError as exc:
            raise LLMProviderError(f"provider error: {exc}") from exc
        try:
            choice = response.choices[0].message
            calls = []
            for tc in choice.tool_calls or []:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except (TypeError, ValueError) as exc:
                    raise LLMProviderError(
                        f"unparseable tool arguments for {tc.function.name!r}"
                    ) from exc
                if not isinstance(args, dict):
                    raise LLMProviderError(
                        f"tool arguments for {tc.function.name!r} are not an object"
                    )
                calls.append(LLMToolCall(id=tc.id, name=tc.function.name, args=args))
            usage = None
            if response.usage is not None:
                usage = LLMUsage(
                    input_tokens=response.usage.prompt_tokens,
                    output_tokens=response.usage.completion_tokens,
                )
            return LLMResponse(text=choice.content or "", tool_calls=calls, usage=usage)
        except (LLMProviderError, LLMAuthError):
            raise
        except Exception as exc:  # noqa: BLE001 - unparseable output fails closed
            raise LLMProviderError(f"cannot parse provider response: {exc}") from exc
