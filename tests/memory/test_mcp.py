"""MCP stdio round-trip on Windows: real server process, real client session."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from memory import structural as structural_mod

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _parse_tool_result(result) -> object:
    """Unwrap CallToolResult across structured/textual encodings."""
    for attr in ("structuredContent", "structured_content"):
        structured = getattr(result, attr, None)
        if isinstance(structured, dict):
            if set(structured) == {"result"}:
                return structured["result"]
            return structured
        if structured is not None:
            return structured
    texts = "".join(getattr(block, "text", "") for block in result.content)
    return json.loads(texts)


def _session_params(db_path: Path, root: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "memory.mcp_server", "--db", str(db_path), "--root", str(root)],
        cwd=str(REPO_ROOT),
    )


async def _exercise(db_path: Path, root: Path) -> dict:
    async with stdio_client(_session_params(db_path, root)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            out = {"tool_names": sorted(t.name for t in tools.tools)}
            callers = await session.call_tool(
                "find_callers", {"name": "refresh_token"}
            )
            out["callers"] = _parse_tool_result(callers)
            definition = await session.call_tool(
                "find_definition", {"name": "refresh_token", "limit": 5}
            )
            out["definition"] = _parse_tool_result(definition)
            search = await session.call_tool("search_symbols", {"query": "token"})
            out["search"] = _parse_tool_result(search)
            unknown = await session.call_tool("find_callers", {"name": "nope"})
            out["unknown"] = _parse_tool_result(unknown)
            # Unknown tools surface as error results, not silent successes.
            bad = await session.call_tool("no_such_tool", {})
            out["bad_tool_is_error"] = bool(getattr(bad, "is_error", False))
            return out


def test_mcp_stdio_round_trip(tmp_path, seeded_fixture):
    """Proves server start -> schemas -> bounded results -> clean shutdown."""
    fixture_dir, _head = seeded_fixture
    db_path = tmp_path / ".agent-memory" / "memory.db"
    structural_mod.index_workspace(fixture_dir, db_path)

    async def bounded():
        return await asyncio.wait_for(_exercise(db_path, fixture_dir), timeout=90)

    out = asyncio.run(bounded())
    # Structural tools must keep working; the exact full surface (now 6 with
    # episodic tools) is owned by test_mcp_episodic.py.
    assert {"find_callers", "find_definition", "search_symbols"} <= set(
        out["tool_names"]
    )

    callers = out["callers"]
    assert {(c["caller"], c["file"]) for c in callers} == {
        ("login_and_refresh", "auth/login.py"),
        ("renew_session", "services/renewal.py"),
    }
    assert out["definition"][0]["file"] == "auth/tokens.py"
    assert {r["name"] for r in out["search"]} >= {"refresh_token"}
    assert out["unknown"] == []
    assert out["bad_tool_is_error"] is True


def test_mcp_server_missing_db_exits_loudly(tmp_path):
    """Proves a missing database fails fast instead of serving empty lies."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "memory.mcp_server",
            "--db",
            str(tmp_path / "absent.db"),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "database not found" in proc.stderr
