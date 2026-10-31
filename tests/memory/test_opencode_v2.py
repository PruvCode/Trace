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
    assert service_mod.opencode_uses_v2_schema(tmp_path / "missing.db") is False
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
    (project / "opencode.json").write_text(json.dumps({"plugins": [{"package": "theirs"}]}))
    adapter = transparent_mod.create_opencode_transparent_adapter()
    adapter._create_opencode_config(project)
    adapter._create_opencode_config(project)
    config = json.loads((project / "opencode.json").read_text(encoding="utf-8"))
    assert config["plugins"] == [{"package": "theirs"}, plugin_mod.plugin_v2_entry()]
    assert "plugin" not in config
    adapter.teardown_integration(project)
    assert json.loads((project / "opencode.json").read_text(encoding="utf-8"))["plugins"] == [{"package": "theirs"}]


DRIVER = """
import { pathToFileURL } from "node:url";
const plugin = (await import(pathToFileURL(process.argv[2]).href)).default;
let hook;
await plugin.setup({ session: { hook: async (name, fn) => { if (name === "context") hook = fn; } } });
const system = [{ type: "text", text: "sys" }];
const event = (sid) => ({ sessionID: sid, system, messages: [{ role: "user", content: [{ type: "text", text: "next task" }] }] });
const a = event("ses_now");
hook(a);
const b = event("ses_now");
hook(b);
const carried = event("ses_now");
carried.messages.unshift({ role: "user", content: [{ type: "text", text: a.messages[0].content[0].text }] });
hook(carried);
console.log(JSON.stringify({ id: plugin.id, first: a.messages[0].content[0].text, counts: [a, b, carried].map((e) => e.messages.length),
  systemParts: system.length }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_v2_plugin_injects_into_messages_on_every_request(tmp_path, env, monkeypatch):
    project, _, _, _ = env
    conn = store_mod.connect(project_mod.db_path(project))
    transcript_mod.record_message(conn, session_id="ses_old", role="user", content="the codename is ZEPHYR")
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
