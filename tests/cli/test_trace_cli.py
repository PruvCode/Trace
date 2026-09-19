"""Tests for the user-facing ``trace`` CLI (daily-use layer).

All filesystem work uses tmp_path (outside OneDrive via basetemp); the CLI
itself never touches the benchmark engine. Fixture: a tiny Python project
with one definition + one caller.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from trace_memory import cli as cli_mod
from trace_memory import project as project_mod

SAMPLE = '''"""Tiny sample project."""


def get_session_timeout():
    return 30


def login():
    return get_session_timeout()
'''


@pytest.fixture()
def sample_project(tmp_path: Path) -> Path:
    proj = tmp_path / "sample"
    proj.mkdir()
    (proj / "session.py").write_text(SAMPLE, encoding="utf-8")
    return proj


def run_cli(*argv: str) -> int:
    return cli_mod.main(list(argv))


def test_init_creates_db_and_indexes(sample_project, capsys):
    assert run_cli("init", str(sample_project)) == 0
    out = capsys.readouterr().out
    assert "initialized" in out
    db = sample_project / ".agent-memory" / "memory.db"
    assert db.exists()
    info = project_mod.status_info(sample_project)
    assert info["symbols"] >= 2  # get_session_timeout + login
    assert info["relationships"] >= 1  # login calls get_session_timeout


def test_init_idempotent_reindexes(sample_project, capsys):
    assert run_cli("init", str(sample_project)) == 0
    capsys.readouterr()
    assert run_cli("init", str(sample_project)) == 0
    assert "re-indexed" in capsys.readouterr().out


def test_status_requires_init(tmp_path, capsys):
    proj = tmp_path / "empty"
    proj.mkdir()
    assert run_cli("status", str(proj)) == 2
    assert "trace init" in capsys.readouterr().err


def test_status_shows_counts(sample_project, capsys):
    run_cli("init", str(sample_project))
    capsys.readouterr()
    assert run_cli("status", str(sample_project)) == 0
    out = capsys.readouterr().out
    assert "symbols" in out and "events" in out


def test_find_and_callers(sample_project, capsys):
    run_cli("init", str(sample_project))
    capsys.readouterr()
    assert run_cli("find", "get_session_timeout", str(sample_project)) == 0
    row = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert row["name"] == "get_session_timeout"
    assert run_cli("callers", "get_session_timeout", str(sample_project)) == 0
    row = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert row["caller"] == "login"


def test_search_symbols(sample_project, capsys):
    run_cli("init", str(sample_project))
    capsys.readouterr()
    assert run_cli("search", "timeout", str(sample_project)) == 0
    assert "get_session_timeout" in capsys.readouterr().out


def test_record_and_events_roundtrip(sample_project, capsys):
    run_cli("init", str(sample_project))
    capsys.readouterr()
    assert run_cli(
        "record", str(sample_project),
        "--type", "observation",
        "--message", "timeout looks wrong",
        "--symbol", "get_session_timeout",
    ) == 0
    result = json.loads(capsys.readouterr().out.strip())
    assert result["id"] == 1
    assert run_cli("events", str(sample_project), "--symbol", "get_session_timeout") == 0
    row = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert row["payload"]["message"] == "timeout looks wrong"


def test_record_rejects_bad_type(sample_project, capsys):
    run_cli("init", str(sample_project))
    capsys.readouterr()
    assert run_cli("record", str(sample_project), "--type", "bogus") == 2
    assert "WHAT" in capsys.readouterr().err


def test_record_rejects_bad_payload(sample_project, capsys):
    run_cli("init", str(sample_project))
    capsys.readouterr()
    assert run_cli("record", str(sample_project), "--type", "observation",
                   "--payload", "[1,2]") == 2


def test_events_rejects_bad_type_filter(sample_project, capsys):
    run_cli("init", str(sample_project))
    capsys.readouterr()
    assert run_cli("events", str(sample_project), "--type", "bogus") == 2


def test_projects_are_isolated(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (b / "m.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    assert run_cli("init", str(a)) == 0
    assert run_cli("init", str(b)) == 0
    assert run_cli("record", str(a), "--type", "decision",
                   "--message", "chose alpha") == 0
    assert project_mod.search_project_events(b, query="alpha") == []
    assert len(project_mod.search_project_events(a, query="alpha")) == 1


def test_mcp_prints_local_config(sample_project, capsys):
    run_cli("init", str(sample_project))
    capsys.readouterr()
    assert run_cli("mcp", str(sample_project)) == 0
    captured = capsys.readouterr()
    config = json.loads(captured.out)
    server = config["mcpServers"]["trace-memory"]
    assert server["command"][1:3] == ["-m", "memory.mcp_server"]
    assert "--db" in server["command"] and "--repo" in server["command"]
    assert "local" in captured.err and "nothing leaves" in captured.err


def test_mcp_requires_init(tmp_path, capsys):
    proj = tmp_path / "plain"
    proj.mkdir()
    assert run_cli("mcp", str(proj)) == 2


def test_report_sections(sample_project, capsys):
    run_cli("init", str(sample_project))
    capsys.readouterr()
    assert run_cli("report", str(sample_project)) == 0
    out = capsys.readouterr().out
    for section in ("Memory", "Measurement", "Privacy", "Episodic" if False else "episodic"):
        assert section in out
    assert "no network calls" in out
    assert "transcript" in out.lower()


def test_history_needs_git_repo(sample_project, capsys):
    run_cli("init", str(sample_project))
    capsys.readouterr()
    assert run_cli("history", str(sample_project)) == 2
    assert "not a git repository" in capsys.readouterr().err


def test_history_reads_git_commits(sample_project, capsys):
    subprocess.run(["git", "init"], cwd=str(sample_project),
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=str(sample_project),
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(sample_project),
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(sample_project),
                   capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(sample_project),
                   capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add session"], cwd=str(sample_project),
                   capture_output=True, check=True)
    run_cli("init", str(sample_project))
    capsys.readouterr()
    assert run_cli("history", str(sample_project)) == 0
    row = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert row["subject"] == "add session"
    assert "get_session_timeout" in row["changed_symbols"]


def test_reset_requires_yes(sample_project, capsys):
    run_cli("init", str(sample_project))
    capsys.readouterr()
    assert run_cli("reset", str(sample_project)) == 2
    assert (sample_project / ".agent-memory" / "memory.db").exists()
    assert run_cli("reset", str(sample_project), "--yes") == 0
    assert not (sample_project / ".agent-memory").exists()


def test_missing_path_helpful(capsys):
    assert run_cli("status", str(Path("does-not-exist-xyz"))) == 2
    err = capsys.readouterr().err
    assert "WHAT" in err and "HOW" in err


def test_init_suggests_gitignore_in_git_repo(sample_project, capsys):
    subprocess.run(["git", "init"], cwd=str(sample_project),
                   capture_output=True, check=True)
    assert run_cli("init", str(sample_project)) == 0
    assert ".gitignore" in capsys.readouterr().out


def test_init_quiet_when_gitignored(sample_project, capsys):
    subprocess.run(["git", "init"], cwd=str(sample_project),
                   capture_output=True, check=True)
    (sample_project / ".gitignore").write_text(".agent-memory/\n", encoding="utf-8")
    assert run_cli("init", str(sample_project)) == 0
    assert ".gitignore" not in capsys.readouterr().out


def test_init_quiet_outside_git_repo(sample_project, capsys):
    assert run_cli("init", str(sample_project)) == 0
    assert ".gitignore" not in capsys.readouterr().out


def test_mcp_server_reaches_cli_database(sample_project, tmp_path):
    """End-to-end: the MCP server serves the database `trace init` built."""
    run_cli("init", str(sample_project))
    run_cli("record", str(sample_project), "--type", "decision",
            "--message", "keep timeout at 30", "--symbol", "get_session_timeout")
    repo_root = Path(__file__).resolve().parent.parent
    probe = tmp_path / "probe_mcp.py"
    probe.write_text(
        "import asyncio\n"
        "import json\n"
        "import sys\n"
        "from mcp import ClientSession, StdioServerParameters\n"
        "from mcp.client.stdio import stdio_client\n"
        "async def go():\n"
        "    params = StdioServerParameters(\n"
        "        command=sys.executable,\n"
        "        args=['-m', 'memory.mcp_server', '--db', sys.argv[1],\n"
        "              '--repo', sys.argv[2]])\n"
        "    async with stdio_client(params) as (read, write):\n"
        "        async with ClientSession(read, write) as session:\n"
        "            await session.initialize()\n"
        "            defs = await session.call_tool(\n"
        "                'find_definition', {'name': 'get_session_timeout'})\n"
        "            evts = await session.call_tool(\n"
        "                'search_events', {'symbol': 'get_session_timeout'})\n"
        "            print(json.dumps(\n"
        "                {'defs': len(defs.content), 'evts': len(evts.content)}))\n"
        "asyncio.run(go())\n",
        encoding="utf-8",
    )
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(probe),
         str(sample_project / ".agent-memory" / "memory.db"),
         str(sample_project)],
        capture_output=True, text=True, timeout=120,
        cwd=str(tmp_path),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert json.loads(proc.stdout.strip()) == {"defs": 1, "evts": 1}
