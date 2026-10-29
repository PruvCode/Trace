"""Regression tests: long-session capture and cross-session retrieval.

Each test drives the production capture path (OpenCodeCaptureService) over a
fake OpenCode database whose rows are shaped like OpenCode 1.18.34's
(verified against a real run in an isolated data directory):

- user text parts: written once, no ``time`` field
- assistant text parts: inserted empty with ``time.start``, completed with
  ``time.end``
- tool parts: ``state.status`` pending -> running -> completed/error
- step-start / step-finish parts around every agent step
- assistant messages get ``time.completed`` when done

Then the next session's injected block is checked for the facts, for noise,
for duplicates and for isolation. Disposable databases only; no network.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from memory import opencode_service as svc_mod
from memory import redact as redact_mod
from memory import session_context as sc_mod
from memory import store as store_mod
from memory import synthetic as synthetic_mod
from memory import transcript as transcript_mod
from memory.opencode_service import CaptureConfig, CaptureError, OpenCodeCaptureService

FACTS = {
    "failed attempt": "raising SESSION_TTL to 3600 broke test_idle_logout_policy",
    "decision": "RefreshMiddleware must run before TimeoutMiddleware",
    "repro": "reproduce locally with FAKE_SKEW=120",
}
NOISE = ("tool-calls", "- assistant: stop")


class FakeOpenCode:
    """Minimal OpenCode database with the columns capture reads."""

    def __init__(self, path: Path):
        self.path = path
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT NOT NULL, parent_id TEXT,
                title TEXT, slug TEXT, model TEXT, agent TEXT,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
            """
        )
        conn.close()
        self._n = 0

    def _exec(self, sql: str, params: tuple) -> int:
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._n:05d}"

    @staticmethod
    def _now() -> int:
        return int(time.time() * 1000)

    def session(self, directory: Path, sid: str | None = None, parent: str | None = None) -> str:
        sid = sid or self._id("ses")
        now = self._now()
        self._exec(
            "INSERT INTO session (id, directory, parent_id, title, time_created, time_updated)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (sid, str(Path(directory).resolve()).replace("\\", "/"), parent, "t", now, now),
        )
        return sid

    def message(self, sid: str, role: str, completed: bool = True) -> str:
        mid = self._id("msg")
        now = self._now()
        when = {"created": now, **({"completed": now} if completed else {})}
        self._exec(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
            (mid, sid, now, now, json.dumps({"role": role, "time": when})),
        )
        return mid

    def complete_message(self, mid: str) -> None:
        conn = sqlite3.connect(self.path)
        data = json.loads(conn.execute("SELECT data FROM message WHERE id = ?", (mid,)).fetchone()[0])
        data["time"]["completed"] = self._now()
        conn.execute("UPDATE message SET data = ? WHERE id = ?", (json.dumps(data), mid))
        conn.commit()
        conn.close()

    def part(self, mid: str, sid: str, data: dict, created_ms: int | None = None) -> str:
        pid = self._id("prt")
        created = created_ms if created_ms is not None else self._now()
        self._exec(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (pid, mid, sid, created, created, json.dumps(data)),
        )
        return pid

    def update_part(self, pid: str, data: dict) -> None:
        self._exec("UPDATE part SET data = ?, time_updated = ? WHERE id = ?",
                   (json.dumps(data), self._now(), pid))

    # -- whole conversations -------------------------------------------------

    def user_turn(self, sid: str, text: str) -> None:
        mid = self.message(sid, "user")
        self.part(mid, sid, {"type": "text", "text": text})  # no "time": written once

    def assistant_text(self, sid: str, text: str) -> None:
        mid = self.message(sid, "assistant")
        self.part(mid, sid, {"type": "step-start"})
        now = self._now()
        self.part(mid, sid, {"type": "text", "text": text, "time": {"start": now, "end": now}})
        self.part(mid, sid, {"type": "step-finish", "reason": "stop", "tokens": {}})

    def tool_step(self, sid: str, tool: str = "read", narration: str | None = None) -> None:
        mid = self.message(sid, "assistant")
        self.part(mid, sid, {"type": "step-start"})
        if narration:
            now = self._now()
            self.part(mid, sid, {"type": "text", "text": narration, "time": {"start": now, "end": now}})
        self.part(mid, sid, {"type": "tool", "tool": tool, "callID": "c",
                             "state": {"status": "completed", "input": {"filePath": "app/x.py"},
                                       "output": "file contents", "time": {"start": 1, "end": 2}}})
        self.part(mid, sid, {"type": "step-finish", "reason": "tool-calls", "tokens": {}})

    def long_session(self, directory: Path, steps: int, narrate: bool = False) -> str:
        sid = self.session(directory)
        self.user_turn(sid, "Users get logged out after idle+activity. Investigate the timeout bug.")
        for i in range(steps):
            self.tool_step(sid, narration=f"Checking file {i} for the timeout path." if narrate else None)
        self.assistant_text(
            sid,
            "Summary: " + FACTS["failed attempt"] + ", so I reverted it. Fix: "
            + FACTS["decision"] + " in app/main.py. Only on staging; " + FACTS["repro"] + ".",
        )
        return sid


@pytest.fixture()
def env(tmp_path):
    """A project dir, its TRACE DB, a fake OpenCode DB and a capture service."""
    project = tmp_path / "proj"
    project.mkdir()
    oc = FakeOpenCode(tmp_path / "opencode.db")
    trace_db = project / ".agent-memory" / "memory.db"
    service = OpenCodeCaptureService(project, oc.path, trace_db, "proj", CaptureConfig())
    state = service._prepare_state()  # first run: cursors start at the (empty) end
    yield project, oc, trace_db, service, state
    service.stop(disable=False)


def _inject(trace_db: Path, current: str = "ses_current") -> str:
    result = sc_mod.get_session_start_context(trace_db, exclude_session_id=current)
    return synthetic_mod.render_user_block(result["items"])


def _rows(trace_db: Path, sql: str, params: tuple = ()) -> list:
    conn = sqlite3.connect(trace_db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _assert_facts_and_no_noise(block: str) -> None:
    for name, fact in FACTS.items():
        assert fact in block, f"{name} lost from injected memory"
    for noise in NOISE:
        assert noise not in block, f"status noise {noise!r} injected"


# 1-3. Retrieval of facts from short, long and noisy sessions ----------------

def test_short_session_three_facts(env):
    project, oc, trace_db, service, state = env
    oc.long_session(project, steps=3)
    service.capture_pass(state)
    _assert_facts_and_no_noise(_inject(trace_db))


def test_thirty_step_session_facts_near_end(env):
    project, oc, trace_db, service, state = env
    sid = oc.long_session(project, steps=30, narrate=True)
    service.capture_pass(state)
    block = _inject(trace_db)
    _assert_facts_and_no_noise(block)
    # The newest narration survives; the oldest yields to the budget.
    assert "Checking file 29" in block
    assert "Checking file 0 " not in block
    assert sid not in block


def test_long_final_summary_keeps_facts_in_its_middle(env):
    # Live regression: a real 1.1k-char final summary lost the fact in its
    # middle to a 500-char head+tail cut.
    project, oc, trace_db, service, state = env
    sid = oc.session(project)
    oc.user_turn(sid, "Summarize the investigation.")
    for _ in range(12):
        oc.tool_step(sid)
    summary = (
        "## Investigation summary\n" + "Files reviewed: notes 1-18, mostly irrelevant. " * 9
        + "\n1. Failed fix: " + FACTS["failed attempt"] + "; reverted.\n"
        + "Background detail that pads the middle of the summary. " * 8
        + "\n2. Decision: " + FACTS["decision"] + ".\n3. Repro: " + FACTS["repro"] + "."
    )
    assert 900 < len(summary) < 1500
    oc.assistant_text(sid, summary)
    service.capture_pass(state)
    _assert_facts_and_no_noise(_inject(trace_db))


def test_legacy_status_rows_never_injected(env):
    project, oc, trace_db, service, state = env
    oc.long_session(project, steps=25)
    service.capture_pass(state)
    # Rows stored by the previous capture version (step-finish as assistant text).
    conn = store_mod.connect(trace_db)
    session_id = _rows(trace_db, "SELECT session_id FROM transcript_messages LIMIT 1")[0][0]
    for n in range(30):
        transcript_mod.record_message(
            conn, session_id=session_id, role="assistant",
            content="tool-calls" if n % 2 else "stop",
            metadata={"part_type": "step-finish", "part_id": f"legacy{n}"},
            source_id=f"part:legacy{n}",
        )
    conn.close()
    _assert_facts_and_no_noise(_inject(trace_db))
    stored = _rows(trace_db, "SELECT COUNT(*) FROM transcript_messages WHERE source_id NOT LIKE 'part:legacy%'"
                             " AND content IN ('tool-calls', 'stop')")[0][0]
    assert stored == 0, "current capture must not store status rows"


# 4. Streaming --------------------------------------------------------------------

def test_incomplete_streaming_text_is_held_then_captured_once(env):
    project, oc, trace_db, service, state = env
    sid = oc.session(project)
    mid = oc.message(sid, "assistant", completed=False)
    start = oc._now()
    pid = oc.part(mid, sid, {"type": "text", "text": "", "time": {"start": start}})
    oc.user_turn(sid, "a later user message")  # rows after the pending part still flow
    service.capture_pass(state)
    held_at = state.cursors["part"]
    assert _rows(trace_db, "SELECT content FROM transcript_messages WHERE role='assistant'") == []
    assert _rows(trace_db, "SELECT content FROM transcript_messages WHERE role='user'") == [("a later user message",)]

    oc.update_part(pid, {"type": "text", "text": "The root cau", "time": {"start": start}})
    service.capture_pass(state)
    assert _rows(trace_db, "SELECT content FROM transcript_messages WHERE role='assistant'") == []
    assert state.cursors["part"] == held_at, "cursor must stay below the streaming part"

    final = "The root cause is middleware order."
    oc.update_part(pid, {"type": "text", "text": final, "time": {"start": start, "end": oc._now()}})
    service.capture_pass(state)
    service.capture_pass(state)
    assert _rows(trace_db, "SELECT content FROM transcript_messages WHERE role='assistant'") == [(final,)]
    assert _rows(trace_db, "SELECT COUNT(*) FROM transcript_messages WHERE role='user'")[0][0] == 1
    assert state.cursors["part"] > held_at


def test_abandoned_part_stops_holding_the_cursor(env):
    project, oc, trace_db, service, state = env
    sid = oc.session(project)
    mid = oc.message(sid, "assistant", completed=False)
    oc.part(mid, sid, {"type": "text", "text": "partial answ", "time": {"start": oc._now()}})
    service.capture_pass(state)
    oc.complete_message(mid)  # turn ended (cancelled) without completing the part
    service.capture_pass(state)
    rows = _rows(trace_db, "SELECT content, metadata FROM transcript_messages WHERE role='assistant'")
    assert len(rows) == 1 and json.loads(rows[0][1]).get("incomplete") is True
    assert state.cursors["part"] == _rows(oc.path, "SELECT MAX(rowid) FROM part")[0][0]


def test_abandoned_partial_text_is_never_injected(env):
    project, oc, trace_db, service, state = env
    sid = oc.session(project)
    oc.user_turn(sid, "what is the root cause?")
    mid = oc.message(sid, "assistant", completed=False)
    oc.part(mid, sid, {"type": "text", "text": "The root cau", "time": {"start": oc._now()}})
    oc.complete_message(mid)
    service.capture_pass(state)
    block = _inject(trace_db)
    assert "what is the root cause?" in block
    assert "The root cau" not in block  # stored (searchable) but cut mid-sentence


def test_timestamp_ties_resolve_to_latest_session(tmp_path):
    winners = []
    for order in (("S1", "S2"), ("S2", "S1")):
        db = tmp_path / f"{order[0]}.db"
        conn = store_mod.connect(db)
        store_mod.init_schema(conn)
        for sid in order:
            for i in range(8):
                transcript_mod.record_message(conn, session_id=sid, role="user", content=f"{sid}-{i}",
                                              timestamp="2026-01-01T00:00:00+00:00")
        conn.close()
        items = sc_mod.get_session_start_context(db)["items"]
        winners.append(items[-1]["session"])
    assert winners == ["S2", "S1"], "the session written last must be treated as the most recent"


def test_rendered_block_respects_the_character_limit(tmp_path):
    from memory import episodic as episodic_mod

    db = tmp_path / "big.db"
    conn = store_mod.connect(db)
    store_mod.init_schema(conn)
    for i in range(12):
        transcript_mod.record_message(
            conn, session_id=f"S{i % 3}", role="assistant" if i % 2 else "user",
            content=("x" * 1490) if i % 2 else ("u" * 495), timestamp=f"2026-01-01T00:00:{i:02d}+00:00")
    for _ in range(5):
        episodic_mod.record_event(conn, type="decision", source="agent", repo="r", payload={"decision": "d" * 490})
    conn.close()
    result = sc_mod.get_session_start_context(db)
    body = "\n".join(synthetic_mod.render_lines(result["items"]))
    assert len(body) <= sc_mod.MAX_CHARS
    assert result["truncated"] is True
    assert any(i["kind"] == "finding" for i in result["items"]), "findings keep their reserved space"


def test_manual_records_are_not_collapsed_by_migration(tmp_path):
    db = tmp_path / "manual.db"
    conn = store_mod.connect(db)
    store_mod.init_schema(conn)
    for text in ("first note", "second note"):
        transcript_mod.record_message(conn, session_id="m", role="user", content=text,
                                      metadata={"message_id": "same", "author": "me"})
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    store_mod.init_schema(conn)
    rows = [r[0] for r in conn.execute("SELECT content FROM transcript_messages ORDER BY id")]
    conn.close()
    assert rows == ["first note", "second note"]


@pytest.mark.xfail(strict=True, reason=(
    "Known limitation: retrieval is recency-based with no task identity. A long, "
    "unrelated session in between fills the transcript budget; recorded findings "
    "and MCP search_transcripts still reach the earlier task."))
def test_unrelated_recent_session_crowds_out_earlier_task(env):
    project, oc, trace_db, service, state = env
    oc.long_session(project, steps=3)  # task X: the three facts
    other = oc.session(project)
    for i in range(12):
        oc.user_turn(other, f"Unrelated CSS question {i}")
    service.capture_pass(state)
    _assert_facts_and_no_noise(_inject(trace_db))


# 5-7. Restarts, failures, replay ---------------------------------------------------

def test_restart_after_successful_capture(env):
    project, oc, trace_db, service, state = env
    oc.long_session(project, steps=5)
    service.capture_pass(state)
    service._save_cursors(state.cursors)
    before = _rows(trace_db, "SELECT COUNT(*) FROM transcript_messages")[0][0]

    restarted = OpenCodeCaptureService(project, oc.path, trace_db, "proj", CaptureConfig())
    state2 = restarted._prepare_state()
    assert state2.cursors == state.cursors
    sid2 = oc.session(project)
    oc.user_turn(sid2, "follow-up question after restart")
    restarted.capture_pass(state2)
    after = _rows(trace_db, "SELECT COUNT(*) FROM transcript_messages")[0][0]
    assert after == before + 2  # the new session row + its user message, nothing replayed twice


def test_restart_after_persistence_failure(env, monkeypatch):
    project, oc, trace_db, service, state = env
    sid = oc.session(project)
    oc.user_turn(sid, "first message")
    oc.user_turn(sid, "SECRETCONTENT message that fails to persist")
    oc.user_turn(sid, "third message")
    real = transcript_mod.record_message
    calls = {"n": 0}

    def flaky(conn, **fields):
        if "SECRETCONTENT" in fields.get("content", ""):
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")
        return real(conn, **fields)

    monkeypatch.setattr(transcript_mod, "record_message", flaky)
    with pytest.raises(CaptureError):
        service.capture_pass(state)
    service._save_cursors(state.cursors)
    assert state.errors == 1 and "database is locked" in state.last_error
    contents = [r[0] for r in _rows(trace_db, "SELECT content FROM transcript_messages WHERE role='user'")]
    assert contents == ["first message"], "nothing after the failed row may be skipped past"
    log = (project / ".trace-service" / "service.log").read_text(encoding="utf-8")
    assert "database is locked" in log and "SECRETCONTENT" not in log

    monkeypatch.setattr(transcript_mod, "record_message", real)
    restarted = OpenCodeCaptureService(project, oc.path, trace_db, "proj", CaptureConfig())
    restarted.capture_pass(restarted._prepare_state())
    contents = [r[0] for r in _rows(trace_db, "SELECT content FROM transcript_messages WHERE role='user'")]
    assert contents == ["first message", "SECRETCONTENT message that fails to persist", "third message"]


def test_failed_commit_rolls_back_progress_and_loses_nothing(env, monkeypatch):
    project, oc, trace_db, service, state = env
    oc.long_session(project, steps=3)
    real = store_mod.set_monitor_cursors

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(store_mod, "set_monitor_cursors", real)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store_mod, "set_monitor_cursors", fail_once)
    with pytest.raises(CaptureError):
        service.capture_pass(state)
    assert _rows(trace_db, "SELECT COUNT(*) FROM transcript_messages")[0][0] == 0, "nothing may be half-committed"
    assert state.cursors == {"session": 0, "message": 0, "part": 0}, "in-memory progress must roll back"

    service.capture_pass(state)
    _assert_facts_and_no_noise(_inject(trace_db))
    sources = [r[0] for r in _rows(trace_db, "SELECT source_id FROM transcript_messages")]
    assert len(sources) == len(set(sources))


def test_replaying_parts_never_duplicates(env):
    project, oc, trace_db, service, state = env
    oc.long_session(project, steps=4)
    service.capture_pass(state)
    count = _rows(trace_db, "SELECT COUNT(*) FROM transcript_messages")[0][0]
    # Cursor rolled back to zero (abrupt kill before any save): full replay.
    replay = OpenCodeCaptureService(project, oc.path, trace_db, "proj", CaptureConfig())
    replay_state = replay._prepare_state()
    replay_state.cursors = {"session": 0, "message": 0, "part": 0}
    replay.capture_pass(replay_state)
    replay.capture_pass(replay_state)
    assert _rows(trace_db, "SELECT COUNT(*) FROM transcript_messages")[0][0] == count


def test_migration_and_capture_share_source_id_format(tmp_path):
    db = tmp_path / "memory.db"
    conn = store_mod.connect(db)
    store_mod.init_schema(conn)
    # Simulate rows from older versions: id only in metadata, or a bare id.
    conn.execute("DROP INDEX idx_transcript_source_id")
    for sid in (None, "prt_1"):
        conn.execute(
            "INSERT INTO transcript_messages (session_id, role, content, timestamp, metadata, source_id)"
            " VALUES ('s', 'assistant', 'hello', 't', ?, ?)",
            (json.dumps({"part_type": "text", "part_id": "prt_1"}), sid),
        )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    store_mod.init_schema(conn)  # migrates: one canonical row remains
    transcript_mod.record_message(
        conn, session_id="s", role="assistant", content="hello",
        metadata={"part_type": "text", "part_id": "prt_1"}, source_id="part:prt_1",
    )
    rows = conn.execute("SELECT source_id FROM transcript_messages").fetchall()
    fts = conn.execute("SELECT COUNT(*) FROM transcript_messages_fts").fetchone()[0]
    conn.close()
    assert [r[0] for r in rows] == ["part:prt_1"]
    assert fts <= 1


# 8. Prefixed tool names ---------------------------------------------------------------

def test_prefixed_memory_tool_calls_captured_when_complete(env):
    project, oc, trace_db, service, state = env
    sid = oc.session(project)
    mid = oc.message(sid, "assistant", completed=False)
    running = {"type": "tool", "tool": "trace-memory_search_events", "callID": "a",
               "state": {"status": "running", "input": {"query": "timeout"}}}
    pid = oc.part(mid, sid, running)
    oc.part(mid, sid, {"type": "tool", "tool": "trace_memory_find_definition", "callID": "b",
                       "state": {"status": "completed", "input": {"name": "refresh_token"}, "output": "[]"}})
    oc.part(mid, sid, {"type": "tool", "tool": "trace-memory_record_event", "callID": "c",
                       "state": {"status": "completed", "input": {"type": "decision"}, "output": "{}"}})
    oc.part(mid, sid, {"type": "tool", "tool": "github_search_events", "callID": "d",
                       "state": {"status": "completed", "input": {}, "output": "x"}})
    service.capture_pass(state)
    tools = sorted(json.loads(r[0])["tool"] for r in _rows(trace_db, "SELECT payload FROM events"))
    assert tools == ["find_definition"]  # running call waits; record_event is self-persisted

    oc.update_part(pid, {**running, "state": {"status": "completed", "input": {"query": "timeout"}, "output": "[]"}})
    service.capture_pass(state)
    service.capture_pass(state)
    rows = _rows(trace_db, "SELECT source, payload FROM events ORDER BY id")
    assert sorted(json.loads(p)["tool"] for _, p in rows) == ["find_definition", "search_events"]
    assert {s for s, _ in rows} == {"capture"}
    assert "Recent findings" not in _inject(trace_db)  # tool activity is never a "finding"


# 9. Injection on later requests (the real plugin, run under node) ------------------

def test_plugin_injects_every_request_without_duplicates(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    from memory import opencode_plugin as plugin_mod

    db = tmp_path / "memory.db"
    conn = store_mod.connect(db)
    store_mod.init_schema(conn)
    # Characters outside the Windows ANSI code page: one "→" once made the
    # helper crash on a piped stdout and silently disabled injection.
    transcript_mod.record_message(conn, session_id="ses_A", role="assistant",
                                  content="Decision → " + FACTS["decision"] + " ✓ 漢字")
    conn.close()
    plugin = tmp_path / "trace-memory.mjs"
    plugin.write_text(
        plugin_mod.render_plugin(sys.executable, str(db), str(tmp_path / "no-opencode.db")),
        encoding="utf-8",
    )
    driver = tmp_path / "driver.mjs"
    driver.write_text(
        """
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
let spawns = 0;
globalThis.Bun = { spawnSync: (cmd, opts) => {
  spawns += 1;
  const r = spawnSync(cmd[0], cmd.slice(1), { cwd: opts.cwd, env: { ...process.env, PYTHONPATH: process.argv[4] } });
  return { exitCode: r.status, stdout: r.stdout, stderr: r.stderr };
} };
const { TraceMemoryPlugin } = await import(pathToFileURL(process.argv[2]).href);
const hooks = await TraceMemoryPlugin({ directory: process.argv[3] });
const hook = hooks["experimental.chat.messages.transform"];
const msg = (role, text) => ({ info: { role, sessionID: "ses_B" }, parts: [{ type: "text", text }] });
const msgC = (role, text) => ({ info: { role, sessionID: "ses_C" }, parts: [{ type: "text", text }] });
const history = [msg("user", "continue the timeout work")];
const seen = [];
let blockText = null;
for (let step = 0; step < 3; step++) {
  const output = { messages: history.map((m) => ({ ...m })) };
  await hook({}, output);
  seen.push({ first: output.messages[0].parts[0].text.slice(0, 40), count: output.messages.length,
              hasFact: output.messages.some((m) => m.parts.some((p) => p.text.includes("RefreshMiddleware"))) });
  if (step === 0 && output.messages[0].parts[0].text.startsWith("[TRACE PROJECT MEMORY")) {
    blockText = output.messages[0].parts[0].text;
  }
  history.push(msg("assistant", "tool step " + step));
}
const already = { messages: [msg("user", "x")] };
await hook({}, already);
await hook({}, already);
// A different session whose request already carries the block: the hook must
// not add a second copy (the carried copy stays the only one).
const carried = { messages: [msgC("user", blockText)] };
await hook({}, carried);
const count = (o) => o.messages.filter((m) => m.parts[0].text.startsWith("[TRACE PROJECT MEMORY")).length;
console.log(JSON.stringify({ spawns, seen, blocks: [count(already), count(carried)] }));
""",
        encoding="utf-8",
    )
    repo_root = str(Path(__file__).resolve().parents[2])
    run = subprocess.run([node, str(driver), str(plugin), str(tmp_path), repo_root],
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout.strip().splitlines()[-1])
    assert report["spawns"] == 2, "python must run once per session (ses_B, ses_C), not once per request"
    # The transform hook fires on every model request and the injected
    # message is never persisted, so every request needs the block again.
    for step in report["seen"]:
        assert step["first"].startswith("[TRACE PROJECT MEMORY"), step
        assert step["hasFact"], "a later agent step lost the memory block"
    assert [s["count"] for s in report["seen"]] == [2, 3, 4]
    assert report["blocks"] == [1, 1], "a request already carrying the block must not get a second copy"


def test_helper_emits_utf8_on_a_piped_stdout(tmp_path):
    import os

    db = tmp_path / "memory.db"
    conn = store_mod.connect(db)
    store_mod.init_schema(conn)
    transcript_mod.record_message(conn, session_id="ses_A", role="assistant",
                                  content="Fix → middleware order ✓ 漢字 🔒")
    conn.close()
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    for module in ("memory.message_context", "memory.session_context"):
        args = ["--db", str(db)]
        if module == "memory.message_context":
            args += ["--opencode-db", str(tmp_path / "none.db"), "--project-dir", str(tmp_path),
                     "--session-id", "ses_B"]
        run = subprocess.run([sys.executable, "-m", module, *args], capture_output=True, env=env,
                             cwd=Path(__file__).resolve().parents[2], timeout=120)
        assert run.returncode == 0, run.stderr.decode("utf-8", "replace")[-400:]
        assert "Fix → middleware order ✓ 漢字 🔒" in run.stdout.decode("utf-8")


# 10. Project isolation ---------------------------------------------------------------

def test_project_isolation(tmp_path):
    oc = FakeOpenCode(tmp_path / "opencode.db")
    services = {}
    for name in ("alpha", "beta"):
        project = tmp_path / name
        project.mkdir()
        svc = OpenCodeCaptureService(project, oc.path, project / ".agent-memory" / "memory.db",
                                     name, CaptureConfig())
        services[name] = (project, svc, svc._prepare_state())
    for name, (project, _, _) in services.items():
        sid = oc.session(project)
        oc.user_turn(sid, f"{name.upper()}-ONLY-MARKER")
    # A sibling directory with a shared prefix must not leak into alpha.
    sib = tmp_path / "alpha-old"
    sib.mkdir()
    oc.user_turn(oc.session(sib), "SIBLING-MARKER")
    for _, svc, state in services.values():
        svc.capture_pass(state)
    alpha = _inject(services["alpha"][1].trace_db_path)
    beta = _inject(services["beta"][1].trace_db_path)
    assert "ALPHA-ONLY-MARKER" in alpha and "BETA-ONLY-MARKER" not in alpha and "SIBLING" not in alpha
    assert "BETA-ONLY-MARKER" in beta and "ALPHA-ONLY-MARKER" not in beta


def test_subagent_sessions_are_not_injected(env):
    project, oc, trace_db, service, state = env
    parent = oc.long_session(project, steps=2)
    child = oc.session(project, parent=parent)
    oc.user_turn(child, "SUBAGENT-INTERNAL prompt")
    service.capture_pass(state)
    block = _inject(trace_db)
    assert "SUBAGENT-INTERNAL" not in block
    _assert_facts_and_no_noise(block)


# 11. Secret redaction ------------------------------------------------------------------

def test_secret_redaction_synthetic_tokens(env):
    # Assembled at runtime: synthetic values, never real credentials.
    tokens = {
        "anthropic": "sk-ant-api03-" + "Ab1" * 30 + "-xyzAA",
        "openai_project": "sk-proj-" + "Ab1_Cd2-" * 8,
        "github_classic_long": "ghp_" + "a" * 40,
        "github_fine_grained": "github_pat_11ABCDEFG0" + "x" * 70,
        "stripe_env": "rk_" + "live_" + "9" * 30,
        "aws_key_id": "AKIA" + "ABCDEFGHIJKLMNOP",
    }
    samples = [
        f"key {tokens['anthropic']} here",
        f"key {tokens['openai_project']}",
        f"token {tokens['github_classic_long']}",
        tokens["github_fine_grained"],
        f"export STRIPE_KEY={tokens['stripe_env']}",
        tokens["aws_key_id"],
        "the db password is hunter2-prod",
        "OPENAI_API_KEY='plainvalue123'",
    ]
    for sample in samples:
        out = redact_mod.redact_text(sample)
        for secret in [*tokens.values(), "hunter2-prod", "plainvalue123"]:
            if secret in sample:
                assert secret not in out, f"leak in {out!r}"
                assert secret[-6:] not in out, f"suffix leak in {out!r}"
    # Ordinary code and prose survive.
    keep = "def refresh_token(session): return session  # key: id"
    assert redact_mod.redact_text(keep) == keep

    project, oc, trace_db, service, state = env
    sid = oc.session(project)
    oc.user_turn(sid, "deploy with " + tokens["anthropic"])
    service.capture_pass(state)
    stored = _rows(trace_db, "SELECT content FROM transcript_messages WHERE role='user'")[0][0]
    assert tokens["anthropic"] not in stored and "REDACTED" in stored


# 12. Setup never damages user configuration ------------------------------------------

@pytest.fixture()
def initialized_project(tmp_path):
    from trace_memory import project as project_mod

    project = tmp_path / "cfgproj"
    project.mkdir()
    (project / "x.py").write_text("def f():\n    pass\n", encoding="utf-8")
    project_mod.init_project(project)
    return project


@pytest.mark.parametrize("content", [
    '{\n  // my model choice\n  "model": "anthropic/claude-opus"\n}\n',
    '{"model": "x",}',
    "not json at all",
])
def test_setup_refuses_unparseable_config_and_leaves_it_unchanged(initialized_project, content):
    from memory.opencode_transparent import OpenCodeTransparentAdapter
    from trace_memory.project import TraceUserError

    path = initialized_project / "opencode.json"
    path.write_bytes(content.encode("utf-8"))
    with pytest.raises(TraceUserError) as info:
        OpenCodeTransparentAdapter()._create_opencode_config(initialized_project)
    assert path.read_bytes() == content.encode("utf-8")
    assert "trace-memory" in info.value.how  # tells the user exactly what to add


def test_setup_refuses_jsonc_config(initialized_project):
    from memory.opencode_transparent import OpenCodeTransparentAdapter
    from trace_memory.project import TraceUserError

    jsonc = initialized_project / "opencode.jsonc"
    jsonc.write_text('{ // comment\n "model": "x" }\n', encoding="utf-8")
    with pytest.raises(TraceUserError):
        OpenCodeTransparentAdapter()._create_opencode_config(initialized_project)
    assert not (initialized_project / "opencode.json").exists()
    assert jsonc.read_text(encoding="utf-8") == '{ // comment\n "model": "x" }\n'


def test_setup_merges_and_teardown_reverses(initialized_project):
    from memory import opencode_plugin as plugin_mod
    from memory.opencode_transparent import OpenCodeTransparentAdapter

    path = initialized_project / "opencode.json"
    user = {"model": "anthropic/claude-opus", "mcp": {"other": {"type": "local", "command": ["x"]}},
            "agent": {"review": {"prompt": "be strict"}}}
    path.write_text(json.dumps(user), encoding="utf-8")
    adapter = OpenCodeTransparentAdapter()
    adapter._create_opencode_config(initialized_project)
    plugin_mod.install_plugin(initialized_project)
    merged = json.loads(path.read_text(encoding="utf-8"))
    assert merged["model"] == user["model"] and merged["agent"] == user["agent"]
    assert set(merged["mcp"]) == {"other", "trace-memory"}

    result = adapter.teardown_integration(initialized_project)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["mcp"] == user["mcp"] and after["model"] == user["model"] and after["agent"] == user["agent"]
    assert result["plugin"]["status"] == "removed"
    assert not plugin_mod.plugin_path(initialized_project).exists()
    assert (initialized_project / ".agent-memory" / "memory.db").exists(), "teardown keeps memory"


def test_setup_preserves_non_ascii_text(initialized_project):
    from memory.opencode_transparent import OpenCodeTransparentAdapter

    path = initialized_project / "opencode.json"
    path.write_text(json.dumps({"model": "x", "note": "café — ünïcödé"}, ensure_ascii=False), encoding="utf-8")
    OpenCodeTransparentAdapter()._create_opencode_config(initialized_project)
    text = path.read_text(encoding="utf-8")
    assert "café — ünïcödé" in text and "\\u00e9" not in text


def test_teardown_keeps_a_plugin_trace_did_not_write(initialized_project):
    from memory import opencode_plugin as plugin_mod

    target = plugin_mod.plugin_path(initialized_project)
    target.parent.mkdir(parents=True)
    target.write_text("// my own plugin\n", encoding="utf-8")
    assert plugin_mod.uninstall_plugin(initialized_project)["status"] == "kept"
    assert target.exists()


# 13-15. Service lifecycle (real detached processes) -------------------------------------

@pytest.fixture()
def live_service(tmp_path):
    project = tmp_path / "svcproj"
    project.mkdir()
    oc = FakeOpenCode(tmp_path / "opencode.db")
    service = OpenCodeCaptureService(project, oc.path, project / ".agent-memory" / "memory.db",
                                     "svcproj", CaptureConfig())
    yield project, oc, service
    service.stop(disable=False)
    for proc in (service._last_proc,):
        if proc is not None and proc.poll() is None:
            proc.kill()


def test_single_instance_when_started_twice(live_service):
    project, oc, service = live_service
    first = service.start()
    assert first["status"] == "started", first
    second = service.start()
    assert second["status"] == "already_running" and second["pid"] == first["pid"]

    # Even a racing second child (bypassing the PID check) cannot run: it loses the lock.
    racer = service._spawn()
    assert racer.wait(timeout=60) == 3
    assert service.status()["pid"] == first["pid"]
    assert svc_mod.process_is_alive(first["pid"])


def test_stale_pid_is_never_treated_as_the_monitor(live_service):
    """After a crash/reboot the PID file can name an unrelated live process.

    It must not be reported as running, must not block a restart, and must
    never be killed by stop().
    """
    project, oc, service = live_service
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        service._write_pid(bystander.pid)
        assert service.is_running() is False
        assert service.stop(disable=False)["status"] == "not_running"
        assert bystander.poll() is None, "stop() killed an unrelated process"

        service._write_pid(bystander.pid)
        started = service.ensure_running()
        assert started["status"] == "started", started
        assert started["pid"] != bystander.pid
        assert service.stop(disable=False)["status"] == "stopped"
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait(timeout=30)


def test_teardown_leaves_no_monitor_running(live_service):
    from memory.opencode_transparent import OpenCodeTransparentAdapter

    project, oc, service = live_service
    started = service.start()
    assert started["status"] == "started", started
    pid = started["pid"]
    result = OpenCodeTransparentAdapter().teardown_integration(project)
    assert result["service"]["status"] == "stopped"
    assert not svc_mod.process_is_alive(pid)
    assert result["service_dir_removed"] is True
    lock = svc_mod.LockHandle(project / svc_mod.SERVICE_DIR_NAME / svc_mod.LOCK_FILE)
    assert lock.acquire(), "a monitor still holds the lock"
    lock.release()


def test_capture_recovers_after_crash(live_service):
    project, oc, service = live_service
    started = service.start()
    assert started["status"] == "started", started
    sid = oc.session(project)
    oc.user_turn(sid, "captured while running")
    deadline = time.time() + 20
    while time.time() < deadline and service.status().get("captured", 0) < 2:
        time.sleep(0.2)
    assert service.status().get("captured", 0) >= 2
    assert service._read_status()["status"] == "running"

    # Crash (or reboot): the monitor dies without its shutdown path.
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(started["pid"])], capture_output=True)
    else:
        import os
        import signal
        os.kill(started["pid"], signal.SIGKILL)
    assert service._wait_for_exit(started["pid"])
    oc.user_turn(sid, "written while capture was down")

    recovered = service.ensure_running()  # what the plugin runs at the next session start
    assert recovered["status"] == "started", recovered
    assert recovered["catch_up"]["status"] == "ok"
    contents = [r[0] for r in _rows(service.trace_db_path,
                                    "SELECT content FROM transcript_messages WHERE role='user'")]
    assert contents == ["captured while running", "written while capture was down"]

    service.stop()
    assert service._read_status()["status"] == "stopped"
    assert service.ensure_running()["status"] == "disabled", "a user stop must stick"
