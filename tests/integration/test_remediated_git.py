"""Git history and evidence stay inside the TRACE project across real entry points."""

import asyncio
import json
import subprocess
import sys
from contextlib import closing

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from memory import episodic, evidence, git_events, store
from trace_memory import cli, project


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Trace Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=missing-hooks", *args],
        cwd=root, text=True, encoding="utf-8",
    ).strip()


@pytest.fixture
def monorepo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    nested = tmp_path / "examples" / "auth-debug"
    nested.mkdir(parents=True)
    source = nested / "auth example.py"
    sibling = tmp_path / "billing.py"
    for revision in (1, 2):
        source.write_text(f"def session_timeout():\n    return {revision}\n", encoding="utf-8")
        sibling.write_text(f"def invoice():\n    return {revision}\n", encoding="utf-8")
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-qm", f"shared revision {revision}")
    expected = git(tmp_path, "rev-parse", "HEAD")
    sibling.write_text("def invoice():\n    return 3\n", encoding="utf-8")
    git(tmp_path, "commit", "-qam", "sibling only")
    return tmp_path, nested, expected


def test_nested_cli_mcp_and_report(monorepo, capsys, monkeypatch):
    root, nested, expected = monorepo
    path = "auth example.py"
    assert cli.main(["init", str(nested)]) == 0
    capsys.readouterr()
    monkeypatch.chdir(root)
    for arguments in (
        [str(nested), "--path", path],
        ["--path", path, str(nested)],
        [str(nested)],
    ):
        assert cli.main(["history", *arguments, "--limit", "1"]) == 0
        row = json.loads(capsys.readouterr().out)
        assert row["sha"] == expected
        assert row["files"] == [{"path": path, "added": 1, "deleted": 1}]
        assert row["changed_symbols"] == ["session_timeout"]
        assert "outside this project" in row["subject"] and row["message"] == ""
    assert cli.main(["history", "--path", f"examples/auth-debug/{path}", "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["sha"] == expected
    assert cli.main(["report", str(nested)]) == 0
    report = capsys.readouterr().out
    assert "Git (facts" in report and expected[:7] in report and "sibling only" not in report
    assert "shared revision 2" not in report

    async def call():
        params = StdioServerParameters(command=sys.executable, args=[
            "-m", "trace_memory.native", "mcp", "mcp", str(nested),
        ])
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("get_git_history", {"path": path, "limit": 1})
                assert not result.is_error
                assert result.structured_content["result"] == [row]

    asyncio.run(asyncio.wait_for(call(), timeout=40))


def test_nested_evidence_and_worktree(monorepo):
    root, nested, expected = monorepo
    path = "auth example.py"
    linked = root.parent / "linked"
    git(root, "worktree", "add", "--detach", str(linked), "HEAD")
    assert git_events.get_git_history(linked / "examples/auth-debug", path=path)[0]["sha"] == expected
    project.init_project(nested)
    with closing(store.connect(project.db_path(nested))) as conn:
        valid = {"type": "git", "commit": expected, "path": path}
        for invalid in ("missing.py", "../../billing.py", "billing.py", "/billing.py"):
            with pytest.raises(ValueError, match="Git evidence"):
                episodic.record_event(conn, type="decision", repo="fixture", project_root=nested,
                                      evidence=[dict(valid, path=invalid)])
        episodic.record_event(conn, type="decision", repo="fixture", project_root=nested, evidence=[valid])
        event = episodic.search_events(conn)[0]
        expanded = evidence.expand(conn, nested, event.evidence_refs[0])
        assert 'return 2' in expanded and '"freshness":"current"' in expanded


def test_unborn_nested_worktree(tmp_path):
    git(tmp_path, "init", "-q")
    child = tmp_path / "first-project"
    child.mkdir()
    assert git_events.get_git_history(child) == []
    assert project.git_summary(child)["is_repo"]
