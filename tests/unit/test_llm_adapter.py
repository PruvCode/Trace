"""LLM adapter tests: deterministic fake client, no network, no keys."""

import pytest

from agent.interface import Budget
from agent.llm_agent import (
    LLMAgent,
    LLMClient,
    LLMProviderError,
    LLMResponse,
    LLMToolCall,
    LLMUsage,
    LLMAuthError,
    OpenAICompatibleClient,
)
from agent.tools import build_core_tools


class FakeListClient(LLMClient):
    """Pops scripted responses; records what the agent sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = []

    def complete(self, messages, tools):
        self.sent.append((list(messages), [t.name for t in tools]))
        if not self._responses:
            raise LLMProviderError("fake client exhausted")
        return self._responses.pop(0)


def _read_write_done(content="hello"):
    return [
        LLMResponse(tool_calls=[LLMToolCall(id="1", name="read_file", args={"path": "a.txt"})]),
        LLMResponse(
            tool_calls=[
                LLMToolCall(
                    id="2", name="write_file", args={"path": "b.txt", "content": content}
                )
            ]
        ),
        LLMResponse(tool_calls=[LLMToolCall(id="3", name="done", args={"summary": "s"})]),
    ]


def test_fake_sequence_read_write_done(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    agent = LLMAgent(FakeListClient(_read_write_done()), model="fake")
    result = agent.run(
        tmp_path, "do it", build_core_tools(tmp_path), Budget(10, 20, 60.0)
    )
    assert result.status == "completed"
    assert result.termination_reason == "done_tool_called"
    assert result.turns == 3
    assert result.tool_calls == 3
    assert [r.name for r in result.tool_log] == ["read_file", "write_file", "done"]
    assert all(r.ok for r in result.tool_log)
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "hello"
    assert result.input_tokens is None and result.output_tokens is None


def test_usage_propagated_exactly(tmp_path):
    responses = _read_write_done()
    responses[0] = LLMResponse(
        tool_calls=responses[0].tool_calls, usage=LLMUsage(10, 5)
    )
    responses[1] = LLMResponse(
        tool_calls=responses[1].tool_calls, usage=LLMUsage(7, 3)
    )
    agent = LLMAgent(FakeListClient(responses), model="fake")
    result = agent.run(
        tmp_path, "do it", build_core_tools(tmp_path), Budget(10, 20, 60.0)
    )
    assert (result.input_tokens, result.output_tokens) == (17, 8)


def test_partial_usage_not_estimated(tmp_path):
    responses = _read_write_done()
    responses[0] = LLMResponse(
        tool_calls=responses[0].tool_calls, usage=LLMUsage(10, 5)
    )
    agent = LLMAgent(FakeListClient(responses), model="fake")
    result = agent.run(
        tmp_path, "do it", build_core_tools(tmp_path), Budget(10, 20, 60.0)
    )
    assert (result.input_tokens, result.output_tokens) == (10, 5)


def test_unknown_tool_fed_back_then_done(tmp_path):
    responses = [
        LLMResponse(tool_calls=[LLMToolCall(id="1", name="nope", args={})]),
        LLMResponse(tool_calls=[LLMToolCall(id="2", name="done", args={"summary": "s"})]),
    ]
    agent = LLMAgent(FakeListClient(responses), model="fake")
    result = agent.run(
        tmp_path, "do it", build_core_tools(tmp_path), Budget(10, 20, 60.0)
    )
    assert result.status == "completed"
    assert result.tool_log[0].ok is False
    assert "unknown tool" in (result.tool_log[0].error or "")
    # The error text went back to the model.
    assert "unknown tool" in agent._client.sent[1][0][-1]["content"]


def test_budget_turns_exhausted(tmp_path):
    agent = LLMAgent(FakeListClient(_read_write_done()), model="fake")
    result = agent.run(
        tmp_path, "do it", build_core_tools(tmp_path), Budget(1, 20, 60.0)
    )
    assert result.status == "budget_exhausted"


def test_budget_tool_calls_exhausted(tmp_path):
    responses = [
        LLMResponse(
            tool_calls=[
                LLMToolCall(id="1", name="read_file", args={"path": "a.txt"}),
                LLMToolCall(id="2", name="done", args={"summary": "s"}),
            ]
        )
    ]
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    agent = LLMAgent(FakeListClient(responses), model="fake")
    result = agent.run(
        tmp_path, "do it", build_core_tools(tmp_path), Budget(10, 1, 60.0)
    )
    assert result.status == "budget_exhausted"


def test_no_tool_calls_stops_cleanly(tmp_path):
    agent = LLMAgent(FakeListClient([LLMResponse(text="looks fine")]), model="fake")
    result = agent.run(
        tmp_path, "do it", build_core_tools(tmp_path), Budget(10, 20, 60.0)
    )
    assert result.status == "completed"
    assert result.termination_reason == "stopped_without_tool_calls"


def test_client_errors_propagate(tmp_path):
    class Exploding(LLMClient):
        def complete(self, messages, tools):
            raise LLMAuthError("bad key")

    agent = LLMAgent(Exploding(), model="fake")
    with pytest.raises(LLMAuthError):
        agent.run(tmp_path, "do it", build_core_tools(tmp_path), Budget(10, 20, 60.0))


def test_fake_exhaustion_raises(tmp_path):
    agent = LLMAgent(FakeListClient([]), model="fake")
    with pytest.raises(LLMProviderError, match="exhausted"):
        agent.run(tmp_path, "do it", build_core_tools(tmp_path), Budget(10, 20, 60.0))


def test_openai_client_missing_key(monkeypatch):
    monkeypatch.delenv("TRACE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMAuthError, match="no API key"):
        OpenAICompatibleClient(model="x")


def test_openai_spec_and_message_translation(tmp_path):
    tools = build_core_tools(tmp_path)
    spec = OpenAICompatibleClient._to_spec(tools[0])
    assert spec["function"]["name"] == "read_file"
    assert spec["function"]["parameters"]["required"] == ["path"]
    msg = OpenAICompatibleClient._to_provider_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "1", "name": "read_file", "args": {"path": "a"}}],
        }
    )
    assert msg["tool_calls"][0]["function"]["arguments"] == '{"path": "a"}'
    tool_msg = OpenAICompatibleClient._to_provider_message(
        {"role": "tool", "tool_call_id": "1", "name": "read_file", "content": "hi"}
    )
    assert tool_msg == {"role": "tool", "tool_call_id": "1", "content": "hi"}
