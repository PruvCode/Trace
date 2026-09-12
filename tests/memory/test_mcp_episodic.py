"""MCP episodic round-trip on Windows: record/search/git_history over stdio."""

import asyncio
import json
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from memory import structural as structural_mod

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXPECTED_TOOLS = [
    "find_callers",
    "find_definition",
    "get_git_history",
    "record_event",
    "search_events",
    "search_symbols",
]


def _parse(result) -> object:
    for attr in ("structured_content", "structuredContent"):
        structured = getattr(result, attr, None)
        if isinstance(structured, dict):
            if set(structured) == {"result"}:
                return structured["result"]
            return structured
        if structured is not None:
            return structured
    return json.loads("".join(getattr(b, "text", "") for b in result.content))


def _params(db_path: Path, repo: Path) -> StdioServerParameters:
    import sys

    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "memory.mcp_server",
            "--db",
            str(db_path),
            "--repo",
            str(repo),
        ],
        cwd=str(REPO_ROOT),
    )


async def _exercise(db_path: Path, repo: Path) -> dict:
    out: dict = {}
    async with stdio_client(_params(db_path, repo)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            out["tool_names"] = sorted(t.name for t in tools.tools)

            recorded = await session.call_tool(
                "record_event",
                {
                    "type": "attempt",
                    "repo": "test-repo",
                    "symbol": "refresh_token",
                    "payload": {"action": "raised timeout", "result": "no effect"},
                },
            )
            out["recorded"] = _parse(recorded)

            found = await session.call_tool(
                "search_events", {"query": "timeout", "limit": 5}
            )
            out["found"] = _parse(found)

            by_type = await session.call_tool(
                "search_events", {"type": "decision"}
            )
            out["by_type"] = _parse(by_type)

            history = await session.call_tool("get_git_history", {"limit": 5})
            out["history"] = _parse(history)

            history_symbol = await session.call_tool(
                "get_git_history", {"symbol": "refresh_token"}
            )
            out["history_symbol"] = _parse(history_symbol)

            # Structural regression inside the same session.
            callers = await session.call_tool(
                "find_callers", {"name": "refresh_token"}
            )
            out["callers"] = _parse(callers)

            bad = await session.call_tool(
                "record_event", {"type": "hallucination", "repo": "test-repo"}
            )
            out["bad_is_error"] = bool(getattr(bad, "is_error", False))
            return out


def test_mcp_episodic_round_trip(tmp_path, seeded_fixture, seeded_history):
    """Proves the exact 6-tool surface end to end with clean shutdown."""
    fixture_dir, _head = seeded_fixture
    history_dir, _shas = seeded_history
    db_path = tmp_path / ".agent-memory" / "memory.db"
    structural_mod.index_workspace(fixture_dir, db_path)

    async def bounded():
        return await asyncio.wait_for(_exercise(db_path, history_dir), timeout=90)

    out = asyncio.run(bounded())
    assert out["tool_names"] == EXPECTED_TOOLS

    assert out["recorded"] == {"id": 1}
    assert len(out["found"]) == 1
    assert out["found"][0]["symbol"] == "refresh_token"
    assert out["found"][0]["payload"] == {
        "action": "raised timeout",
        "result": "no effect",
    }
    assert out["found"][0]["source"] == "agent"
    assert out["by_type"] == []

    subjects = [h["subject"] for h in out["history"]]
    assert subjects[0] == "renew sessions via refresh_token"
    assert len(out["history"]) == 3
    assert {h["sha"] for h in out["history_symbol"]} < {
        h["sha"] for h in out["history"]
    }  # c3 excluded: call-only

    assert {(c["caller"], c["file"]) for c in out["callers"]} == {
        ("login_and_refresh", "auth/login.py"),
        ("renew_session", "services/renewal.py"),
    }
    assert out["bad_is_error"] is True


def test_mcp_git_history_without_repo_is_error(tmp_path, seeded_fixture):
    """Proves unconfigured git state fails safely instead of guessing."""
    import sys

    fixture_dir, _head = seeded_fixture
    db_path = tmp_path / "memory.db"
    structural_mod.index_workspace(fixture_dir, db_path)
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    async def _run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "memory.mcp_server",
                "--db",
                str(db_path),
                "--repo",
                str(not_a_repo),
            ],
            cwd=str(REPO_ROOT),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("get_git_history", {})

    result = asyncio.run(asyncio.wait_for(_run(), timeout=90))
    assert bool(getattr(result, "is_error", False)) is True
