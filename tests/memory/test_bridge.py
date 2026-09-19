"""MCP bridge tests: lifecycle, calls, failures, tool parity (Phase 4.2)."""

import sys
from pathlib import Path

import pytest

from agent.tools import build_core_tools, call_tool
from benchmark.mcp_bridge import MCPBridgeError, MCPBridgeSession
from memory import structural as structural_mod

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MEMORY_TOOL_NAMES = {
    "find_definition",
    "find_callers",
    "search_symbols",
    "record_event",
    "search_events",
    "get_git_history",
    "record_transcript_message",
    "search_transcripts",
    "get_transcript_session",
}


@pytest.fixture()
def indexed_db(tmp_path, seeded_fixture):
    fixture_dir, _head = seeded_fixture
    db_path = tmp_path / ".agent-memory" / "memory.db"
    structural_mod.index_workspace(fixture_dir, db_path)
    return db_path


def _command(db_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "memory.mcp_server",
        "--db",
        str(db_path),
        "--repo",
        str(db_path.parent),
    ]


def test_bridge_start_call_close(indexed_db):
    bridge = MCPBridgeSession(_command(indexed_db), cwd=str(REPO_ROOT))
    try:
        bridge.start()
        assert set(bridge.tool_names()) == MEMORY_TOOL_NAMES
        defs = {t.name: t for t in bridge.tool_definitions()}
        assert set(defs) == MEMORY_TOOL_NAMES
        # Same logical interface as core tools: works through call_tool.
        ok, result, record = call_tool(defs["find_callers"], name="refresh_token")
        assert ok is True
        assert {c["caller"] for c in result} == {
            "login_and_refresh",
            "renew_session",
        }
        assert record.name == "find_callers"
    finally:
        bridge.close()
        bridge.close()  # idempotent
    assert bridge.alive() is False


def test_bridge_bad_command_fails_loudly(tmp_path):
    bridge = MCPBridgeSession(
        ["definitely-not-a-real-executable-xyz"],
        cwd=str(REPO_ROOT),
        startup_timeout=10.0,
        startup_retries=1,
    )
    try:
        with pytest.raises(MCPBridgeError, match="mcp startup failed"):
            bridge.start()
    finally:
        bridge.close()


def test_bridge_unknown_tool_raises(indexed_db):
    bridge = MCPBridgeSession(_command(indexed_db), cwd=str(REPO_ROOT))
    try:
        bridge.start()
        with pytest.raises(MCPBridgeError, match="mcp tool error"):
            bridge.call("no_such_tool", {})
    finally:
        bridge.close()


def test_tool_parity_core_identical_memory_additive(tmp_path, indexed_db):
    """Mandatory fairness: reference = same core tools + memory tools."""
    core_a = {t.name: (t.description, t.json_schema) for t in build_core_tools(tmp_path)}
    core_b = {t.name: (t.description, t.json_schema) for t in build_core_tools(tmp_path)}
    assert core_a == core_b
    assert set(core_a) == {"read_file", "write_file", "done"}
    bridge = MCPBridgeSession(_command(indexed_db), cwd=str(REPO_ROOT))
    try:
        bridge.start()
        memory_names = set(bridge.tool_names())
        assert memory_names == MEMORY_TOOL_NAMES
        assert set(core_a) & memory_names == set()
        assert set(core_a) | memory_names == set(core_a) | MEMORY_TOOL_NAMES
    finally:
        bridge.close()
