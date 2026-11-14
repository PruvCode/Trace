"""Tests for the user-facing ``trace`` CLI (daily-use layer).

All filesystem work uses tmp_path (outside OneDrive via basetemp); the CLI
itself never touches the benchmark engine. Fixture: a tiny Python project
with one definition + one caller.
"""

import io
import json
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from memory import store
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


def test_task_commands_link_and_redact_legacy_records(sample_project, capsys):
    path = str(sample_project)
    assert run_cli("init", path) == 0
    capsys.readouterr()
    assert run_cli("task", "create", "--project", path, "--title", "Login timeout", "--subsystem", "auth") == 0
    task = json.loads(capsys.readouterr().out)
    assert run_cli("task", "list", "--project", path, "--subsystem", "auth", "--limit", "1") == 0
    assert json.loads(capsys.readouterr().out) == task
    assert run_cli("transcript", "record", "--project", path, "--session-id", "s1",
                   "--role", "user", "--content", "The timeout repeats") == 0
    message_id = json.loads(capsys.readouterr().out)["id"]
    assert run_cli("record", path, "--type", "attempt", "--message", "timeout attempt") == 0
    event_id = json.loads(capsys.readouterr().out)["id"]
    assert run_cli("task", "link", task["id"], str(event_id), "--project", path) == 0
    assert json.loads(capsys.readouterr().out)["id"] == event_id
    assert run_cli("record", path, "--type", "investigation", "--task", task["id"],
                   "--transcript-evidence", str(message_id), "--message", "timeout finding") == 0
    new_id = json.loads(capsys.readouterr().out)["id"]
    assert run_cli("events", path, "--task", task["id"], "--query", "timeout") == 0
    assert {json.loads(line)["id"] for line in capsys.readouterr().out.splitlines()} == {event_id, new_id}

    fake_secret = "ghp_" + "Ab9Z" * 9
    with closing(store.connect(project_mod.db_path(sample_project))) as conn, conn:
        conn.execute("UPDATE events SET payload = ? WHERE id = ?",
                     (json.dumps({"message": f"Legacy {fake_secret}", fake_secret: "test"}), event_id))
        conn.execute("UPDATE transcript_messages SET content = ? WHERE id = ?",
                     (f"Legacy {fake_secret}", message_id))
    assert run_cli("task", "show", task["id"], "--project", path) == 0
    shown = capsys.readouterr().out
    assert fake_secret not in shown
    assert json.loads(shown)["events"][1]["evidence"] == [{"type": "transcript", "message_id": message_id}]
    assert run_cli("report", path) == 0
    assert fake_secret not in capsys.readouterr().out
    assert run_cli("context", "timeout", "--project", path) == 0
    context = capsys.readouterr().out
    assert fake_secret not in context and "REDACTED" in context


def test_init_idempotent_reindexes(sample_project, capsys):
    assert run_cli("init", str(sample_project)) == 0
    capsys.readouterr()
    assert run_cli("init", str(sample_project)) == 0
    assert "re-indexed" in capsys.readouterr().out


def test_privacy_and_preview_follow_project_controls(sample_project, tmp_path, monkeypatch, capsys):
    from memory import opencode_plugin, opencode_service, transcript

    path = str(sample_project)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "synthetic-open-code"))
    monkeypatch.delenv("TRACE_TASK_AWARE", raising=False)
    assert run_cli("privacy", "--project", path) == 0
    state = capsys.readouterr().out
    assert "not initialized" in state and "Automatic recall: not configured" in state
    assert not project_mod.db_path(sample_project).exists()
    assert run_cli("init", path) == 0
    opencode_plugin.install_plugin(sample_project, opencode_db_path=tmp_path / "unused.db")
    service = sample_project / opencode_service.SERVICE_DIR_NAME
    stale = service / opencode_service.PID_FILE
    stale.write_text("2147483647", encoding="utf-8")
    assert run_cli("test-capture", "on", "--project", path) == 0
    capsys.readouterr()
    assert run_cli("privacy", "--project", path) == 0
    state = capsys.readouterr().out
    assert "Automatic recall: plugin installed" in state
    assert "OpenCode capture: stopped" in state and stale.exists()
    assert "Selected test recording: opted in" in state

    secret = "ghp_" + "Ab9Z" * 9
    with closing(store.connect(project_mod.db_path(sample_project))) as conn:
        row = transcript.record_message(conn, session_id="prior", role="user", content="Orchid decision approved.")
        with conn:
            conn.execute("UPDATE transcript_messages SET content = ? WHERE id = ?", (f"Orchid {secret}", row))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO("Orchid café\0".encode("utf-8"))))
    assert run_cli("preview", "--query-stdin", "--session", "new", "--project", path) == 0
    block = capsys.readouterr().out
    assert "Orchid" in block and "REDACTED" in block and secret not in block
    assert not (sample_project / ".agent-memory" / "retrieval.log").exists()
    assert run_cli("service", "stop", "--project", path) == 0
    capsys.readouterr()
    assert run_cli("privacy", "--project", path) == 0
    state = capsys.readouterr().out
    assert "OpenCode capture: paused" in state and "Automatic recall: plugin installed" in state
    assert run_cli("teardown", "opencode", "--project", path) == 0
    capsys.readouterr()
    assert run_cli("privacy", "--project", path) == 0
    assert "Automatic recall: disabled" in capsys.readouterr().out
    assert run_cli("preview", "--project", path) == 0
    assert capsys.readouterr().out == "No automatic memory block would be included.\n"
    assert project_mod.db_path(sample_project).exists()


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
    assert server["command"] == sys.executable
    assert server["args"] == ["-I", "-m", "trace_memory.native", "mcp", "mcp", str(sample_project.resolve())]
    assert "local" in captured.err and "model provider" in captured.err


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
    assert (sample_project / ".trace-service" / "integration.disabled").is_file()
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
    ignore = sample_project / ".gitignore"
    ignore.write_text(".agent-memory/\n", encoding="utf-8")
    assert run_cli("init", str(sample_project)) == 0
    output = capsys.readouterr().err
    assert "add .trace-service/ to .gitignore" in output
    assert ignore.read_text(encoding="utf-8") == ".agent-memory/\n"
    ignore.write_text(".agent-memory/\n.trace-service/\n", encoding="utf-8")
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


def test_backup_copies_committed_wal_and_keeps_existing_destination(tmp_path, capsys):
    import sqlite3
    from contextlib import closing

    from trace_memory import project

    folder = tmp_path / "project"
    folder.mkdir()
    project.init_project(folder)
    destination = tmp_path / "private snapshot.db"
    with closing(sqlite3.connect(project.db_path(folder))) as active:
        assert active.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        active.execute("PRAGMA wal_autocheckpoint=0")
        active.execute("CREATE TABLE backup_probe (message TEXT)")
        active.execute("INSERT INTO backup_probe VALUES ('latest committed data')")
        active.commit()
        assert cli_mod.main(["backup", str(destination), "--project", str(folder)]) == 0
        assert "backup written" in capsys.readouterr().out
    with closing(sqlite3.connect(destination)) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT message FROM backup_probe").fetchone() == ("latest committed data",)
    digest_before = destination.read_bytes()
    assert cli_mod.main(["backup", str(destination), "--project", str(folder)]) == 2
    assert destination.read_bytes() == digest_before


def test_reset_preserves_memory_when_capture_cannot_stop(sample_project, monkeypatch):
    from types import SimpleNamespace

    assert run_cli("init", str(sample_project)) == 0
    db = project_mod.db_path(sample_project)
    before = db.read_bytes()
    service = SimpleNamespace(stop=lambda **_: {"status": "error", "message": "capture holds lock"})
    monkeypatch.setattr(project_mod.opencode_service_mod, "get_service", lambda *_: service)
    assert run_cli("reset", str(sample_project), "--yes") == 2
    assert db.read_bytes() == before


def test_backup_failure_keeps_final_filename_absent(sample_project, tmp_path, monkeypatch):
    import sqlite3
    from types import SimpleNamespace

    assert run_cli("init", str(sample_project)) == 0
    target = tmp_path / "backup.db"
    connect = sqlite3.connect

    def fail_backup(*_args, **_kwargs):
        assert not target.exists()
        raise sqlite3.OperationalError("synthetic backup interruption")

    def open_db(*args, **kwargs):
        if kwargs.get("uri"):
            return SimpleNamespace(backup=fail_backup, close=lambda: None)
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", open_db)
    assert run_cli("backup", str(target), "--project", str(sample_project)) == 2
    assert not target.exists()
    assert not list(tmp_path.glob(".trace-backup-*"))
