"""Exercise generated hook commands and the installed MCP process across hosts."""

import asyncio
import json
import os
import subprocess
import sys
import tomllib
from contextlib import closing

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from memory import episodic, store, task_library
from trace_memory import integrations
from trace_memory.cli import main
from trace_memory.native import MAX_PAYLOAD_BYTES, receipt_path
from trace_memory.project import db_path


HOSTS = ("codex", "claude", "antigravity")


def cli(action, project, *hosts):
    return main([action, "--project", str(project), *(arg for host in hosts for arg in ("--agent", host))])


def files(project, host):
    return [project / name for name in integrations._paths(host)]


def invoke(project, host, event, payload, *, generated=True, env=None):
    if generated and host != "antigravity":
        config = json.loads(files(project, host)[0].read_text("utf-8"))
        handler = config["hooks"][event][-1]["hooks"][0]
        if "args" in handler:
            command = [handler["command"], *handler["args"]]
        elif os.name == "nt":
            command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", handler["command"]]
        else:
            command = ["/bin/sh", "-c", handler["command"]]
    elif generated:
        config = json.loads(files(project, host)[0].read_text("utf-8"))
        hook_command = config["trace-memory"][event][0]["command"]
        if os.name == "nt":
            command = f'cmd.exe /d /s /c "{hook_command}"'
        else:
            command = ["/bin/sh", "-c", hook_command]
    else:
        command = [sys.executable, "-I", "-m", "trace_memory.native", "hook", host, str(project), event]
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    result = subprocess.run(command, input=raw, cwd=project, capture_output=True, env=env,
                            check=True, timeout=15)
    return json.loads(result.stdout)


def envelope(project, host, event, session, text, turn="turn-1"):
    if host == "antigravity":
        return {"workspacePaths": [str(project)], "conversationId": session, "lastUserInput": text,
                "invocationNum": 0, "transcriptPath": str(project / "never_read.jsonl")}
    return {
        "cwd": str(project), "session_id": session, "hook_event_name": event,
        "turn_id" if host == "codex" else "prompt_id": turn,
        "last_assistant_message" if event == "Stop" else "prompt": text,
    }


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "Project space café & team’s"
    root.mkdir()
    (root / "sample.py").write_text("def sample_symbol():\n    return 1\n", encoding="utf-8")
    return root.resolve()


def test_cross_agent_capture_recall_and_stdio(project, tmp_path, capsys):
    assert cli("install", project, *HOSTS) == 0
    output = json.loads(capsys.readouterr().out)
    assert all(output[host]["status"] == "configured" for host in HOSTS)
    observed = {path: path.read_bytes() for host in HOSTS for path in files(project, host)}
    assert cli("install", project, *HOSTS) == 0
    output = json.loads(capsys.readouterr().out)
    assert all(output[host]["status"] == "unchanged" for host in HOSTS)
    assert all(path.read_bytes() == content for path, content in observed.items())

    # A workspace module and inherited PYTHONPATH must never be imported by the hooks.
    (project / "json.py").write_text("raise AssertionError('workspace import')\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(project)}
    submit = "UserPromptSubmit"
    token = "ghp_" + "z" * 36
    agent_id = "same-id"
    coder = envelope(project, "codex", submit, agent_id, f"Plan the lighthouse deployment. api_key={token}")
    assert not invoke(project, "codex", submit, coder, env=env)
    assert not invoke(project, "codex", submit, coder)
    response = envelope(project, "codex", "Stop", agent_id, "Lighthouse deployment uses a COBALT_702 gate. →")
    assert invoke(project, "codex", "Stop", response) == {}
    assert invoke(project, "codex", "Stop", response) == {}

    newcomer = envelope(project, "claude", submit, agent_id, "Recall the lighthouse deployment")
    context = invoke(project, "claude", submit, newcomer)["hookSpecificOutput"]["additionalContext"]
    assert "COBALT_702" in context and "untrusted data" in context and token not in context
    assert len(context) <= 3_000
    assert invoke(project, "claude", "Stop", envelope(
        project, "claude", "Stop", agent_id, "Use IRIDIUM_409 for the lighthouse deployment."
    )) == {}
    agy = envelope(project, "antigravity", "PreInvocation", agent_id, "Recall the lighthouse deployment")
    assert "IRIDIUM_409" in invoke(project, "antigravity", "PreInvocation", agy)["injectSteps"][0]["ephemeralMessage"]
    assert invoke(project, "antigravity", "PreInvocation", agy).get("injectSteps")
    with closing(store.connect(db_path(project))) as conn:
        rows = conn.execute(
            "SELECT session_id, role, content FROM transcript_messages WHERE role <> 'system' ORDER BY id"
        ).fetchall()
        markers = conn.execute("SELECT count(*) FROM transcript_messages WHERE role = 'system'").fetchone()[0]
    assert [tuple(row)[:2] for row in rows] == [
        ("codex:same-id", "user"), ("codex:same-id", "assistant"),
        ("claude:same-id", "user"), ("claude:same-id", "assistant"), ("antigravity:same-id", "user"),
    ]
    assert token not in rows[0]["content"] and "REDACTED" in rows[0]["content"]
    assert markers == 3

    command = tomllib.loads(files(project, "codex")[1].read_text("utf-8"))["mcp_servers"]["trace-memory"]
    other = tmp_path / "different-workspace"
    other.mkdir()

    async def exercise():
        params = StdioServerParameters(**command, cwd=str(other))
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert {"search_transcripts", "record_event", "get_evidence"} <= names
            result = await client.call_tool("search_transcripts", {"query": "COBALT_702"})
            result_text = result.model_dump_json()
            assert "COBALT_702" in result_text and token not in result_text
            assert cli("uninstall", project, "codex") == 0
            stopped = await client.call_tool("search_transcripts", {"query": "COBALT_702"})
            assert stopped.is_error and "COBALT_702" not in stopped.model_dump_json()

        generic = integrations.mcp_entry(project)
        async with stdio_client(StdioServerParameters(**generic, cwd=str(other))) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                result = await client.call_tool("find_definition", {"name": "sample_symbol"})
                assert not result.is_error and "sample.py" in result.model_dump_json()

    asyncio.run(asyncio.wait_for(exercise(), 60))
    assert invoke(project, "codex", submit, coder, generated=False) == {}
    assert db_path(project).exists()


def test_workspace_and_payload_boundaries(project, tmp_path):
    assert cli("install", project, *HOSTS) == 0
    data = envelope(project, "codex", "UserPromptSubmit", "session", "Excluded conversation")
    nested = project / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    for cwd in (tmp_path, nested, "relative"):
        assert invoke(project, "codex", "UserPromptSubmit", {**data, "cwd": str(cwd)}) == {}
    assert invoke(project, "codex", "UserPromptSubmit", {**data, "hook_event_name": "Stop"}) == {}
    assert invoke(project, "codex", "UserPromptSubmit", {**data, "agent_id": "subagent"}) == {}
    assert invoke(project, "codex", "UserPromptSubmit", b"{" + b" " * MAX_PAYLOAD_BYTES) == {}
    assert invoke(project, "codex", "UserPromptSubmit", b"invalid") == {}
    assert invoke(project, "codex", "UserPromptSubmit", data,
                  env={**os.environ, "TRACE_DISABLE_HOOKS": "1"}) == {}
    agy = envelope(project, "antigravity", "PreInvocation", "session", "Private multi-root prompt")
    agy["workspacePaths"].append(str(tmp_path))
    assert invoke(project, "antigravity", "PreInvocation", agy) == {}
    with closing(store.connect(db_path(project))) as conn:
        assert conn.execute("SELECT count(*) FROM transcript_messages").fetchone()[0] == 0


def test_native_session_can_recall_its_bound_task(project):
    assert cli("install", project, "codex") == 0
    event = "UserPromptSubmit"
    assert invoke(project, "codex", event, envelope(project, "codex", event, "task-session", "Initialize")) == {}
    with closing(store.connect(db_path(project))) as conn:
        task = task_library.create_task(conn, "Release readiness")["id"]
        task_library.bind_session(conn, task, "codex:task-session")
        episodic.record_event(
            conn, repo="fixture", type="decision", task_id=task,
            payload={"decision": "The release inspection uses a COBALT_702 gate.", "status": "observed"},
        )
    result = invoke(project, "codex", event, envelope(project, "codex", event, "task-session", "Recall release inspection", "turn-2"))
    assert "COBALT_702" in result["hookSpecificOutput"]["additionalContext"]


def test_preserves_settings_and_handles_conflicts(project, capsys):
    hooks, codex = files(project, "codex")
    hooks.parent.mkdir()
    foreign = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "check"}]}]},
               "description": "Keep"}
    hooks.write_text(json.dumps(foreign), encoding="utf-8")
    prefix = b'# owner configuration\r\nmodel = "custom"\r\n'
    codex.write_bytes(prefix)
    assert cli("install", project, "codex") == 0
    assert codex.read_bytes().startswith(prefix)
    assert cli("doctor", project, "codex") == 0
    capsys.readouterr()
    assert cli("rollback", project, "codex") == 0
    assert codex.read_bytes() == prefix and json.loads(hooks.read_bytes()) == foreign

    original = b'{"mcpServers":{"trace-memory":{"command":"user-command"}}}'
    mcp = files(project, "claude")[1]
    mcp.write_bytes(original)
    assert cli("install", project, "claude") == 1
    assert mcp.read_bytes() == original and not files(project, "claude")[0].exists()
    hooks.write_bytes(b'{"hooks":{},"hooks":{}}')
    assert cli("install", project, "codex") == 1
    assert hooks.read_bytes() == b'{"hooks":{},"hooks":{}}'
    assert codex.read_bytes() == prefix


def test_interrupted_install_is_recoverable_and_rollback_restores_previous(project, monkeypatch, capsys):
    original_write = integrations.atomic_write_text
    hooks, mcp = files(project, "claude")

    def interrupted(path, text):
        if path == mcp:
            raise OSError("simulated interrupted second write")
        original_write(path, text)

    monkeypatch.setattr(integrations, "atomic_write_text", interrupted)
    assert cli("install", project, "claude") == 1
    assert hooks.exists() and not mcp.exists()
    assert cli("doctor", project, "claude") == 1
    payload = envelope(project, "claude", "UserPromptSubmit", "session", "Do not record pending setup")
    assert invoke(project, "claude", "UserPromptSubmit", payload) == {}
    monkeypatch.setattr(integrations, "atomic_write_text", original_write)
    assert cli("install", project, "claude") == 0
    old = hooks.read_bytes(), mcp.read_bytes()

    old_command = integrations._command
    monkeypatch.setattr(integrations, "_command", lambda *args: [*old_command(*args)[:1], "-B", *old_command(*args)[1:]])
    assert cli("update", project, "claude") == 0
    assert (hooks.read_bytes(), mcp.read_bytes()) != old
    assert cli("rollback", project, "claude") == 0
    assert (hooks.read_bytes(), mcp.read_bytes()) == old

    hooks_config = json.loads(hooks.read_bytes())
    hooks_config["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 1
    hooks.write_text(json.dumps(hooks_config), encoding="utf-8")
    assert cli("uninstall", project, "claude") == 1
    assert receipt_path(project, "claude").exists() and mcp.read_bytes() == old[1]


def test_relies_on_local_paths_and_does_not_detect_missing_hosts(project, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(integrations, "agent_binary", lambda host: None)
    assert cli("install", project) == 0
    assert {value["status"] for value in json.loads(capsys.readouterr().out).values()} == {"not-detected"}
    outside = tmp_path / "external.json"
    outside.write_bytes(b"{}")
    mcp = files(project, "claude")[1]
    os.link(outside, mcp)
    assert cli("install", project, "claude") == 1
    assert outside.read_bytes() == b"{}"


def test_opencode_first_run_and_preserved_pause(project, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "opencode-empty"))
    monkeypatch.setattr(integrations, "_opencode_major", lambda _root: 2)
    assert cli("install", project, "opencode") == 0
    result = json.loads(capsys.readouterr().out)["opencode"]
    assert result["status"] == "awaiting-first-run"
    config_file = project / "opencode.json"
    config = json.loads(config_file.read_bytes())
    assert "trace-memory" in config["mcp"]["servers"] and "plugins" in config

    assert main(["service", "stop", "--project", str(project)]) == 0
    capsys.readouterr()
    assert cli("update", project, "opencode") == 0
    result = json.loads(capsys.readouterr().out)["opencode"]
    assert result["monitoring"]["status"] == "disabled"
    assert cli("uninstall", project, "opencode") == 0
    assert cli("install", project, "opencode") == 0
    capsys.readouterr()
    assert cli("doctor", project, "opencode") == 0
    status = json.loads(capsys.readouterr().out)["opencode"]
    assert not status["capture_disabled"] and not status["recall_disabled"]
    assert db_path(project).is_file()


def test_doctor_identifies_unavailable_runtime(project, monkeypatch, capsys):
    command = integrations._command
    missing = project / "old-runtime" / "python"
    monkeypatch.setattr(integrations, "_command", lambda *args: [str(missing), *command(*args)[1:]])
    assert cli("install", project, "claude") == 0
    capsys.readouterr()
    assert cli("doctor", project, "claude") == 1
    assert json.loads(capsys.readouterr().out)["claude"]["status"] == "runtime-missing"
    monkeypatch.setattr(integrations, "_command", command)
    assert cli("update", project, "claude") == 0
    capsys.readouterr()
    assert cli("doctor", project, "claude") == 0
