"""MockAgent memory-behavior tests: tolerant with/without memory tools."""

from agent.interface import Budget, ToolDef
from agent.mock_agent import MockAgent, TARGET_FILE


def _session(tmp_path, buggy=True):
    content = (
        "SESSION_TIMEOUT_MINUTES = 30\n"
        "def get_session_timeout():\n"
        + (
            "    return 15  # BUG: should return SESSION_TIMEOUT_MINUTES\n"
            if buggy
            else "    return SESSION_TIMEOUT_MINUTES\n"
        )
    )
    (tmp_path / TARGET_FILE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / TARGET_FILE).write_text(content, encoding="utf-8")


def _core_only(tmp_path):
    from agent.tools import build_core_tools

    return build_core_tools(tmp_path)


def test_memory_behavior_without_memory_tools(tmp_path):
    _session(tmp_path)
    agent = MockAgent(behavior="memory")
    result = agent.run(tmp_path, "fix", _core_only(tmp_path), Budget(10, 20, 60.0))
    assert result.status == "completed"
    assert result.turns == 3
    assert "SESSION_TIMEOUT_MINUTES\n" in (tmp_path / TARGET_FILE).read_text(
        encoding="utf-8"
    )


def test_memory_behavior_uses_memory_tools(tmp_path):
    _session(tmp_path)
    seen = []

    def search_symbols(query, limit=10):
        seen.append(("search_symbols", query))
        return [{"name": "get_session_timeout"}]

    def record_event(type, repo, symbol=None, payload=None, **rest):
        seen.append(("record_event", type, symbol))
        return {"id": 1}

    tools = _core_only(tmp_path) + [
        ToolDef(name="search_symbols", description="s", json_schema={}, handler=search_symbols),
        ToolDef(name="record_event", description="r", json_schema={}, handler=record_event),
    ]
    agent = MockAgent(behavior="memory")
    result = agent.run(tmp_path, "fix", tools, Budget(10, 20, 60.0))
    assert result.status == "completed"
    assert result.turns == 5
    assert seen == [
        ("search_symbols", "timeout"),
        ("record_event", "observation", "get_session_timeout"),
    ]
    assert "return SESSION_TIMEOUT_MINUTES" in (tmp_path / TARGET_FILE).read_text(
        encoding="utf-8"
    )


def test_unknown_behavior_still_rejected():
    import pytest

    with pytest.raises(ValueError, match="unknown mock behavior"):
        MockAgent(behavior="nope")
