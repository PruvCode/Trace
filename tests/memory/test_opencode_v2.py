"""OpenCode 2.x support: session_message capture, config, and the V2 plugin."""

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from memory import opencode_plugin as plugin_mod
from memory import opencode_service as service_mod
from memory import opencode_transparent as transparent_mod
from memory import store as store_mod
from memory import transcript as transcript_mod
from trace_memory import project as project_mod

V2_SCHEMA = """
CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT NOT NULL, parent_id TEXT, title TEXT, slug TEXT,
    agent TEXT, model TEXT, time_created INTEGER);
CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL, seq INTEGER,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
"""


class Fake:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(V2_SCHEMA)
        self.conn.commit()
        self.n = 0

    def session(self, directory: Path) -> str:
        self.n += 1
        sid = f"ses_{self.n}"
        self.conn.execute("INSERT INTO session_v2 (id, directory, title) VALUES (?, ?, 't')",
                          (sid, str(directory).replace("\\", "/")))
        self.conn.commit()
        return sid

    def message(self, sid: str, kind: str, data: dict, created: int | None = None) -> str:
        self.n += 1
        mid = f"msg_{self.n}"
        now = created or int(time.time() * 1000)
        self.conn.execute("INSERT INTO session_message VALUES (?, ?, ?, 1, ?, ?, ?)",
                          (mid, sid, kind, now, now, json.dumps(data)))
        self.conn.commit()
        return mid

    def update(self, mid: str, data: dict):
        self.conn.execute("UPDATE session_message SET data = ? WHERE id = ?", (json.dumps(data), mid))
        self.conn.commit()


def assistant(text: str | None, done: bool = True) -> dict:
    return {"time": {"created": 1, **({"completed": 2} if done else {})},
            "content": [{"type": "text", "text": text}] if text else []}


@pytest.fixture()
def env(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    project_mod.init_project(project)
    fake = Fake(tmp_path / "opencode.db")
    service = service_mod.OpenCodeCaptureService(
        project_path=project, project_name="project",
        trace_db_path=project_mod.db_path(project), opencode_db_path=fake.path,
        config=service_mod.CaptureConfig(),
    )
    state = service._prepare_state()
    return project, fake, service, state


def _rows(project):
    conn = store_mod.connect(project_mod.db_path(project))
    rows = [(r["role"], r["content"]) for r in conn.execute(
        "SELECT role, content FROM transcript_messages WHERE role <> 'system' ORDER BY id")]
    conn.close()
    return rows


def test_schema_is_detected_from_table_names(tmp_path, env):
    _, fake, service, _ = env
    assert service_mod.opencode_uses_v2_schema(fake.path) is True
    assert service_mod.opencode_uses_v2_schema(tmp_path / "missing.db") is None
    service._validate_schema()


def test_user_and_finished_assistant_text_are_captured_once(env):
    project, fake, service, state = env
    sid = fake.session(project)
    fake.message(sid, "user", {"text": "the codename is ZEPHYR"})
    fake.message(sid, "assistant", assistant("noted"))
    fake.message(sid, "idle", {"outcome": "succeeded"})
    service.capture_pass(state)
    service.capture_pass(state)
    assert _rows(project) == [("user", "the codename is ZEPHYR"), ("assistant", "noted")]


def test_v2_captures_terminal_shell_parts_without_copying_output(env):
    from contextlib import closing
    from memory import captured_outcomes, episodic

    project, fake, service, state = env
    sid = fake.session(project)
    with closing(store_mod.connect(project_mod.db_path(project))) as conn:
        since = captured_outcomes.configure(conn, "on")["enabled_since_ms"]

    def shell(call_id, **overrides):
        state = {"status": "completed", "input": {"command": "pytest -q"},
                 "content": [{"type": "text", "text": "private stdout\n\x1b[32m3 passed\x1b[0m in 0.02s"}]}
        return {"type": "tool", "name": "shell", "id": call_id, "state": {**state, **overrides}}

    final = assistant(None)
    final["content"] = [
        shell("call-1"),
        shell("call-2", status="error", metadata={"exit": 2}),
        shell("call-3", status="error", metadata={"aborted": True}),
        shell("call-4", status="running"),
        shell("call-5", input={"command": "pytest; echo ignored"}),
        shell("call-6", input={"command": "pytest", "workdir": str(project.parent)}),
        shell("call-7", content=[{"type": "text", "text": "unrecognized output"}]),
        shell("call-8", metadata={"status": "running"}),
        shell("call-9", metadata={"timeout": True}),
    ]
    first = assistant(None, done=False)
    first["content"] = final["content"]
    mid = fake.message(sid, "assistant", first, created=since + 1)
    other = fake.session(project.parent)
    fake.message(other, "assistant", final, created=since + 1)
    service.capture_pass(state)
    with closing(store_mod.connect(project_mod.db_path(project))) as conn:
        assert episodic.search_events(conn) == []
    fake.update(mid, final)
    service.capture_pass(state)
    with closing(store_mod.connect(project_mod.db_path(project))) as conn:
        assert episodic.search_events(conn) == []  # the finished message still has a running tool
    final["content"][3] = shell("call-4", status="error", metadata={"aborted": True})
    fake.update(mid, final)
    service.capture_pass(state)
    replay = service._prepare_state()
    replay.cursors["message"] = 0
    service.capture_pass(replay)
    with closing(store_mod.connect(project_mod.db_path(project))) as conn:
        events = episodic.search_events(conn)
        assert [event.payload["outcome"] for event in events] == ["completed", "interrupted"]
        assert [event.payload["basis"] for event in events] == ["pytest_summary", "exit_code"]
        assert events[0].payload["counts"] == {"passed": 3}
        assert all(event.task_id is None and len(event.evidence) == 1 for event in events)
        for index, event in enumerate(events, 1):
            row = conn.execute("SELECT content, metadata FROM transcript_messages WHERE id = ?",
                               (event.evidence[0]["message_id"],)).fetchone()
            assert json.loads(row["metadata"])["upstream_tool_id"] == f"call-{index}"
            assert "private stdout" not in row["content"]


def test_streaming_reply_waits_for_completion_and_later_rows_still_flow(env):
    project, fake, service, state = env
    sid = fake.session(project)
    mid = fake.message(sid, "assistant", assistant("partial", done=False))
    fake.message(sid, "user", {"text": "next"})
    service.capture_pass(state)
    assert _rows(project) == [("user", "next")]
    fake.update(mid, assistant("the full reply"))
    service.capture_pass(state)
    assert ("assistant", "the full reply") in _rows(project)


def test_other_projects_are_not_captured(tmp_path, env):
    project, fake, service, state = env
    other = fake.session(tmp_path / "other")
    fake.message(other, "user", {"text": "private"})
    service.capture_pass(state)
    assert _rows(project) == []


def test_restart_resumes_from_saved_cursors(env):
    project, fake, service, state = env
    sid = fake.session(project)
    fake.message(sid, "user", {"text": "one"})
    service.capture_pass(state)
    fake.message(sid, "user", {"text": "two"})
    service.capture_pass(service._prepare_state())
    assert _rows(project) == [("user", "one"), ("user", "two")]


def test_setup_uses_the_v2_plugin_key_and_teardown_removes_it(tmp_path, env, monkeypatch):
    project, fake, _, _ = env
    monkeypatch.setattr(service_mod, "default_opencode_db_path", lambda: fake.path)
    from trace_memory import project as project_mod

    path = project / "opencode.json"
    other = {"type": "local", "command": ["other"]}
    prior = {"type": "local", "enabled": False, "command": project_mod.mcp_server_command(project)}
    path.write_text(json.dumps({"plugins": [{"package": "theirs"}],
                                "mcp": {"trace-memory": prior, "servers": {"other": other}, "timeout": 9000}}))
    adapter = transparent_mod.create_opencode_transparent_adapter()
    adapter._create_opencode_config(project)
    adapter._create_opencode_config(project)
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["plugins"] == [{"package": "theirs"}, plugin_mod.plugin_v2_entry()]
    assert "plugin" not in config and "trace-memory" not in config["mcp"]
    assert config["mcp"]["servers"]["trace-memory"] == {
        "type": "local", "disabled": True, "codemode": False, "command": [*project_mod.mcp_server_command(project, opencode=True), "--tools", "core"],
    }
    adapter.teardown_integration(project)
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["plugins"] == [{"package": "theirs"}]
    assert config["mcp"] == {"servers": {"other": other}, "timeout": 9000}


DRIVER = """
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { pathToFileURL } from "node:url";
const prompt = "Find ZEPHYR 🧪";
const original = childProcess.spawnSync;
let safeTransport = false;
childProcess.spawnSync = (file, args, opts) => {
  safeTransport = args.includes("--query-stdin") && !args.some((arg) => arg.includes("ZEPHYR")) && opts.input === prompt;
  return original(file, args, opts);
};
syncBuiltinESMExports();
const plugin = (await import(pathToFileURL(process.argv[2]).href)).default;
let hook;
await plugin.setup({ session: { hook: async (name, fn) => { if (name === "context") hook = fn; } } });
const system = [{ type: "text", text: "sys" }];
const event = (sid) => ({ sessionID: sid, system, messages: [{ role: "user", content: [{ type: "text", text: prompt }] }] });
const a = event("ses_now");
hook(a);
const b = event("ses_now");
hook(b);
const carried = event("ses_now");
carried.messages.unshift({ role: "user", content: [{ type: "text", text: a.messages[0].content[0].text }] });
hook(carried);
console.log(JSON.stringify({ id: plugin.id, first: a.messages[0].content[0].text, counts: [a, b, carried].map((e) => e.messages.length),
  systemParts: system.length, safeTransport }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_v2_plugin_injects_into_messages_on_every_request(tmp_path, env, monkeypatch):
    project, _, _, _ = env
    conn = store_mod.connect(project_mod.db_path(project))
    transcript_mod.record_message(conn, session_id="ses_old", role="user", content="the codename is ZEPHYR",
                                  timestamp="2025-01-01T00:00:00Z")
    for index in range(12):
        transcript_mod.record_message(conn, session_id="ses_newer", role="user", content=f"Unrelated message {index}")
    conn.close()
    plugin_mod.install_plugin(project, tmp_path / "opencode.db")
    index = plugin_mod.plugin_v2_dir(project) / "index.js"
    index.write_text(index.read_text(encoding="utf-8").replace('"--ensure-capture",', ""), encoding="utf-8")
    driver = tmp_path / "driver.mjs"
    driver.write_text(DRIVER, encoding="utf-8")
    run = subprocess.run(["node", str(driver), str(index)], capture_output=True, text=True, timeout=60,
                         env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])})
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout.strip().splitlines()[-1])
    assert report["id"] == "trace-memory"
    assert report["safeTransport"] is True
    assert report["first"].startswith("[TRACE PROJECT MEMORY") and "ZEPHYR" in report["first"]
    assert report["counts"] == [2, 2, 2]  # every request, never a second copy
    assert report["systemParts"] == 1  # memory stays out of the trusted system prompt


def test_v2_plugin_is_removed_with_the_rest(tmp_path, env):
    project, _, _, _ = env
    plugin_mod.install_plugin(project, tmp_path / "opencode.db")
    assert (plugin_mod.plugin_v2_dir(project) / "index.js").exists()
    plugin_mod.uninstall_plugin(project)
    assert not plugin_mod.plugin_v2_dir(project).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs privileges on Windows")
def test_project_reached_through_a_symlink_is_still_captured(tmp_path, env):
    project, fake, service, state = env
    link = tmp_path / "link"
    link.symlink_to(project)
    fake.message(fake.session(link), "user", {"text": "via link"})
    service.capture_pass(state)
    assert _rows(project) == [("user", "via link")]


def test_directory_comparison_uses_the_resolved_path(env, monkeypatch):
    project, _, service, _ = env
    real_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: project if self.name == "alias" else real_resolve(self, *a, **k))
    assert service._is_project_dir(str(project.parent / "alias")) is True
    assert service._is_project_dir(str(project.parent / "another")) is False


def test_lone_surrogate_row_does_not_wedge_capture(env):
    project, fake, service, state = env
    sid = fake.session(project)
    fake.conn.execute("INSERT INTO session_message VALUES ('m1', ?, 'user', 1, 1, 1, ?)", (sid, '{"text":"cut \\ud83d"}'))
    fake.conn.commit()
    fake.message(sid, "user", {"text": "after"})
    service.capture_pass(state)
    assert [r[1] for r in _rows(project)] == ["cut \ufffd", "after"]


def test_long_reply_is_held_until_the_session_moves_on(env):
    project, fake, service, state = env
    sid = fake.session(project)
    old = int(time.time() * 1000) - 60 * 60 * 1000  # an hour-long reply is still alive
    mid = fake.message(sid, "assistant", assistant("partial", done=False), created=old)
    service.capture_pass(state)
    assert _rows(project) == []
    fake.update(mid, assistant("the final reply"))
    service.capture_pass(state)
    assert _rows(project) == [("assistant", "the final reply")]


def test_abandoned_reply_is_stored_as_incomplete_once_the_session_moved_on(env):
    project, fake, service, state = env
    sid = fake.session(project)
    fake.message(sid, "assistant", assistant("cut off", done=False))
    fake.message(sid, "idle", {"outcome": "failed"})
    service.capture_pass(state)
    assert _rows(project) == [("assistant", "cut off")]


def test_rows_reusing_deleted_rowids_are_not_lost(env):
    project, fake, service, state = env
    sid = fake.session(project)
    for text in ("one", "two", "three"):
        fake.message(sid, "user", {"text": text})
    service.capture_pass(state)
    fake.conn.execute("DELETE FROM session_message WHERE rowid > 1")
    fake.conn.commit()
    service.capture_pass(state)  # the monitor polls: it sees the table shrink
    fake.message(sid, "user", {"text": "four"})
    service.capture_pass(state)
    assert ("user", "four") in _rows(project)


def test_running_monitor_follows_an_upgrade_of_the_database(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    project_mod.init_project(project)
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, time_created INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
    """)
    conn.commit()
    conn.close()
    service = service_mod.OpenCodeCaptureService(
        project_path=project, project_name="project", trace_db_path=project_mod.db_path(project),
        opencode_db_path=db, config=service_mod.CaptureConfig())
    state = service._prepare_state()
    service.capture_pass(state)
    assert service._is_v2() is False
    fake = Fake.__new__(Fake)
    fake.path, fake.conn, fake.n = db, sqlite3.connect(db), 0
    fake.conn.executescript(V2_SCHEMA)
    fake.conn.commit()
    service.capture_pass(state)
    assert service._is_v2() is True
    fake.message(fake.session(project), "user", {"text": "after upgrade"})
    service.capture_pass(state)
    assert _rows(project) == [("user", "after upgrade")]


def test_unusable_session_directory_does_not_stop_capture(env):
    project, fake, service, state = env
    fake.session(Path("bad\x00dir"))
    fake.message(fake.session(project), "user", {"text": "fine"})
    service.capture_pass(state)
    assert _rows(project) == [("user", "fine")]


def test_unreadable_database_keeps_the_existing_plugin_key(tmp_path, env, monkeypatch):
    project, _, _, _ = env
    monkeypatch.setattr(service_mod, "default_opencode_db_path", lambda: tmp_path / "missing" / "x.db")
    entry = plugin_mod.plugin_v2_entry()
    (project / "opencode.json").write_text(json.dumps({"plugins": [entry]}))
    transparent_mod.create_opencode_transparent_adapter()._create_opencode_config(project)
    assert json.loads((project / "opencode.json").read_text(encoding="utf-8"))["plugins"] == [entry]
