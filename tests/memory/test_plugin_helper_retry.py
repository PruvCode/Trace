"""Regression tests: bounded retrieval-helper retry (Phase 0, step 0F).

Drives the REAL plugin JavaScript under Node with a stubbed
``Bun.spawnSync`` so the plugin's caching/retry logic is exercised end to
end. A failing helper used to be cached as "empty" permanently, so a single
transient failure (locked database, helper crash) disabled injection for
that entire session. The plugin now retries once; a clean "nothing to
inject" run stays final and never retried.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from memory import opencode_plugin as plugin_mod
from memory import store as store_mod
from memory import transcript as transcript_mod

FACT_TEXT = "Decision: RefreshMiddleware must run before TimeoutMiddleware"
LABEL = "[TRACE PROJECT MEMORY"


def _trace_db_with(tmp: Path, *, empty: bool) -> Path:
    db = tmp / "memory.db"
    conn = store_mod.connect(db)
    store_mod.init_schema(conn)
    if not empty:
        transcript_mod.record_message(conn, session_id="ses_A", role="assistant",
                                      content=FACT_TEXT)
    conn.close()
    return db


DRIVER_TEMPLATE = r"""
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
const LABEL_START = "[TRACE PROJECT MEMORY";
let spawns = 0;
// failTimes: how many of the first helper spawns fail (exit 1, no stats).
globalThis.Bun = { spawnSync: (cmd, opts) => {
  spawns += 1;
  if (spawns <= FAIL_TIMES) {
    return { exitCode: 1, stdout: Buffer.from(""), stderr: Buffer.from("") };
  }
  const r = spawnSync(cmd[0], cmd.slice(1),
    { cwd: opts.cwd, env: { ...process.env, PYTHONPATH: process.argv[4] } });
  return { exitCode: r.status, stdout: r.stdout || Buffer.from(""),
           stderr: r.stderr || Buffer.from("") };
} };
const { TraceMemoryPlugin } = await import(pathToFileURL(process.argv[2]).href);
const hooks = await TraceMemoryPlugin({ directory: process.argv[3] });
const hook = hooks["experimental.chat.messages.transform"];
const msg = (role, text) => ({ info: { role, sessionID: "ses_B" },
                               parts: [{ type: "text", text }] });
const history = [msg("user", "continue the timeout work")];
const seen = [];
for (let step = 0; step < REQUESTS; step++) {
  const output = { messages: history.map((m) => ({ ...m })) };
  await hook({}, output);
  const first = output.messages[0] ? output.messages[0].parts[0].text : "";
  const copies = output.messages.filter(
    (m) => m.parts[0] && m.parts[0].text.startsWith(LABEL_START)).length;
  seen.push({ injected: first.startsWith(LABEL_START), copies });
  history.push(msg("assistant", "tool step " + step));
}
console.log(JSON.stringify({ spawns, seen }));
"""


@pytest.fixture()
def helper_env(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    repo_root = Path(__file__).resolve().parents[2]
    return node, repo_root


def _run_scenario(helper_env, tmp: Path, trace_db: Path, requests: int,
                  fail_times: int, *, name: str) -> dict:
    node, repo_root = helper_env
    plugin = tmp / f"trace-memory-{name}.mjs"
    plugin.write_text(
        plugin_mod.render_plugin(sys.executable, str(trace_db), str(tmp / "no-opencode.db")),
        encoding="utf-8",
    )
    driver = tmp / f"driver_{name}.mjs"
    driver.write_text(
        DRIVER_TEMPLATE.replace("FAIL_TIMES", str(fail_times))
                       .replace("REQUESTS", str(requests)),
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items()}
    env["PYTHONPATH"] = str(repo_root)
    run = subprocess.run(
        [node, str(driver), str(plugin), str(tmp), str(repo_root)],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(tmp))
    assert run.returncode == 0, run.stderr[-1500:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def _retrieval_log(trace_db: Path) -> list[dict]:
    log = trace_db.parent / "retrieval.log"
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def test_recoverable_failure_is_retried_once_then_injects(helper_env, tmp_path):
    """One transient helper failure must not disable memory for the session."""
    db = _trace_db_with(tmp_path, empty=False)
    report = _run_scenario(helper_env, tmp_path, db, requests=4, fail_times=1, name="retry1")
    assert report["spawns"] == 2, report["seen"]  # failed spawn + one repeat
    # Injected from the second attempt on, into every later request (the
    # synthetic message is never persisted), and never as a duplicate.
    injected = [e["injected"] for e in report["seen"]]
    assert injected == [False, True, True, True], report["seen"]
    assert [e["copies"] for e in report["seen"]] == [0, 1, 1, 1], report["seen"]


def test_permanent_failure_is_bounded(helper_env, tmp_path):
    """A helper failing every time retries once, then gives up (2 spawns max)."""
    db = _trace_db_with(tmp_path, empty=False)
    report = _run_scenario(helper_env, tmp_path, db, requests=4, fail_times=99, name="perm")
    assert report["spawns"] == 2, report["seen"]
    assert all(not e["injected"] for e in report["seen"])


def test_clean_empty_result_is_never_retried(helper_env, tmp_path):
    """Helper ran and injected nothing: final result, exactly one spawn."""
    db = _trace_db_with(tmp_path, empty=True)
    report = _run_scenario(helper_env, tmp_path, db, requests=4, fail_times=0, name="empty")
    assert report["spawns"] == 1
    assert all(not e["injected"] for e in report["seen"])


def test_request_already_carrying_block_never_gets_a_second_copy(helper_env, tmp_path):
    db = _trace_db_with(tmp_path, empty=False)
    node, repo_root = helper_env
    plugin = tmp_path / "already.mjs"
    plugin.write_text(
        plugin_mod.render_plugin(sys.executable, str(db), str(tmp_path / "no-opencode.db")),
        encoding="utf-8")
    driver = tmp_path / "driver_already.mjs"
    driver.write_text(
        r"""
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
let spawns = 0;
globalThis.Bun = { spawnSync: (cmd, opts) => {
  spawns += 1;
  const r = spawnSync(cmd[0], cmd.slice(1),
    { cwd: opts.cwd, env: { ...process.env, PYTHONPATH: process.argv[4] } });
  return { exitCode: r.status, stdout: r.stdout || Buffer.from(""),
           stderr: r.stderr || Buffer.from("") };
} };
const { TraceMemoryPlugin } = await import(pathToFileURL(process.argv[2]).href);
const hooks = await TraceMemoryPlugin({ directory: process.argv[3] });
const hook = hooks["experimental.chat.messages.transform"];
const copied = process.argv[5];
const carried = { info: { role: "user", sessionID: "ses_B" },
                  parts: [{ type: "text", text: copied }] };
const pre = { messages: [carried] };
await hook({}, pre);
await hook({}, pre);
console.log(JSON.stringify({ spawns, count: pre.messages.length,
  copies: pre.messages.filter((m) => m.parts[0].text.startsWith("[TRACE PROJECT")).length }));
""",
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items()}
    env["PYTHONPATH"] = str(repo_root)
    # Build a real block by running the assembly helper ourselves.
    from memory import message_context as message_context_mod

    result = message_context_mod.get_message_start_context(
        db, opencode_db_path=tmp_path / "none.db", project_dir=tmp_path, session_id="ses_B")
    block = result["text"]
    assert block.startswith(LABEL)
    run = subprocess.run(
        [node, str(driver), str(plugin), str(tmp_path), str(repo_root), block],
        capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))
    assert run.returncode == 0, run.stderr[-1500:]
    report = json.loads(run.stdout.strip().splitlines()[-1])
    assert report["copies"] == 1, report  # never a duplicate injected
    assert report["count"] == 1, report  # the carried block was left alone


def test_diagnostic_line_reports_outcome(helper_env, tmp_path):
    db = _trace_db_with(tmp_path, empty=False)
    _run_scenario(helper_env, tmp_path, db, requests=4, fail_times=1, name="diag")
    entries = _retrieval_log(db)
    assert entries, "plugin wrote no diagnostic lines"
    first = entries[0]
    for key in ("ts", "sessionID", "channel", "spawned", "injected", "items",
                "chars", "capture", "latencyMs", "error", "reason"):
        assert key in first, (key, first)
    assert first["sessionID"] == "ses_B" and first["spawned"] is True
    injected_later = [e for e in entries if e.get("injected") is True]
    assert len(injected_later) == 3, entries  # every request after the retry
    assert all(e["chars"] > 0 for e in injected_later)
    assert injected_later[0]["spawned"] and injected_later[0]["items"] >= 1
    assert sum(e["spawned"] for e in entries) == 2  # helper not rerun per request
    # Diagnostics never contain memory content (only sizes/flags).
    exported = json.dumps(entries)
    assert FACT_TEXT not in exported, "diagnostic log leaked memory content"


def test_missing_session_identity_spawns_nothing(helper_env, tmp_path):
    """Unresolvable session identity fails open before any helper run."""
    db = _trace_db_with(tmp_path, empty=False)
    node, repo_root = helper_env
    plugin = tmp_path / "noid.mjs"
    plugin.write_text(
        plugin_mod.render_plugin(sys.executable, str(db), str(tmp_path / "no-opencode.db")),
        encoding="utf-8")
    driver = tmp_path / "driver_noid.mjs"
    driver.write_text(
        r"""
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
let spawns = 0;
globalThis.Bun = { spawnSync: () => { spawns += 1;
  return { exitCode: 1, stdout: Buffer.from(""), stderr: Buffer.from("") }; } };
const { TraceMemoryPlugin } = await import(pathToFileURL(process.argv[2]).href);
const hooks = await TraceMemoryPlugin({ directory: process.argv[3] });
const hook = hooks["experimental.chat.messages.transform"];
const no_owner = { messages: [{ parts: [{ type: "text", text: "no sessionID anywhere" }] }] };
await hook({}, no_owner);
const not_array = {};
await hook({}, not_array);
console.log(JSON.stringify({ spawns, grew: no_owner.messages.length !== 1 }));
""",
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items()}
    env["PYTHONPATH"] = str(repo_root)
    run = subprocess.run(
        [node, str(driver), str(plugin), str(tmp_path), str(repo_root)],
        capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))
    assert run.returncode == 0, run.stderr[-1500:]
    report = json.loads(run.stdout.strip().splitlines()[-1])
    assert report["spawns"] == 0, report
    assert report["grew"] is False, report
