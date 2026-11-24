"""Native hooks commit atomically under contention and honor per-host controls."""

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Timer

import pytest

from trace_memory import cli, project
from trace_memory.native import HOOK_HOSTS, MAX_PAYLOAD_BYTES


def hook(root, host, session, text, turn="1"):
    if host == "antigravity":
        event = "PreInvocation"
        payload = {"workspacePaths": [str(root)], "conversationId": session, "lastUserInput": text}
    else:
        event = "UserPromptSubmit"
        payload = {"hook_event_name": event, "cwd": str(root), "session_id": session, "prompt": text,
                   "turn_id": turn, "prompt_id": turn}
    return subprocess.run(
        [sys.executable, "-m", "trace_memory.native", "hook", host, str(root), event],
        input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=20, check=True,
    )


def rows(root):
    database = project.db_path(root)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        return conn.execute("SELECT session_id, role, content FROM transcript_messages ORDER BY id").fetchall()


def install(root, host, capsys):
    assert cli.main(["install", "--agent", host, "--project", str(root)]) == 0
    capsys.readouterr()


def test_contention_retries_and_reports_exhaustion(tmp_path, capsys):
    install(tmp_path, "codex", capsys)
    with closing(sqlite3.connect(project.db_path(tmp_path), check_same_thread=False)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        timer = Timer(1.7, writer.rollback)
        timer.start()
        try:
            result = hook(tmp_path, "codex", "delayed", "Invoice credits require an audit.")
        finally:
            timer.join()
        assert "unavailable" not in result.stderr
        assert [role for _, role, _ in rows(tmp_path)] == ["system", "user"]
        writer.execute("BEGIN IMMEDIATE")
        result = hook(tmp_path, "codex", "exhausted", "A private synthetic prompt under a persistent lock.")
        writer.rollback()
    assert "database_busy" in result.stderr and "private synthetic" not in result.stderr
    assert all(session != "codex:exhausted" for session, _, _ in rows(tmp_path))
    health = project.privacy_info(tmp_path)["hosts"]["codex"]
    assert health["last_saved"]["status"] == "saved" and health["last_failure"]["error"] == "database_busy"
    hook(tmp_path, "codex", "exhausted", "A private synthetic prompt under a persistent lock.")
    hook(tmp_path, "codex", "exhausted", "A private synthetic prompt under a persistent lock.")
    assert [role for session, role, _ in rows(tmp_path) if session == "codex:exhausted"] == ["system", "user"]


def test_invalid_native_payloads_are_visible_without_retaining_content(tmp_path, capsys):
    install(tmp_path, "codex", capsys)
    for payload in ('{"private fixture content"', '"private fixture content"', 'x' * (MAX_PAYLOAD_BYTES + 1)):
        result = subprocess.run(
            [sys.executable, "-m", "trace_memory.native", "hook", "codex", str(tmp_path), "UserPromptSubmit"],
            input=payload, capture_output=True, text=True, encoding="utf-8", timeout=20, check=True,
        )
        assert json.loads(result.stdout) == {}
        assert result.stderr.strip() == "TRACE native capture unavailable (invalid_payload)"
        assert rows(tmp_path) == []
        health = project.privacy_info(tmp_path)["hosts"]["codex"]
        assert health["last_failure"]["error"] == "invalid_payload" and health["last_saved"] is None
    hook(tmp_path, "codex", "after-rejection", "Fixture password=synthetic-guard-931.")
    assert "***REDACTED***" in rows(tmp_path)[-1][2]
    assert "synthetic-guard-931" not in rows(tmp_path)[-1][2]


@pytest.mark.parametrize("host", HOOK_HOSTS)
def test_native_pause_reset_and_uninstall(tmp_path, host, capsys):
    install(tmp_path, host, capsys)
    hook(tmp_path, host, "first", "Invoice credits require an audit.")
    expected = project.preview_context(tmp_path, "later", "Invoice credits", agent=host)
    delivered = json.loads(hook(tmp_path, host, "later", "Invoice credits").stdout)
    if host == "antigravity":
        assert delivered["injectSteps"][0]["ephemeralMessage"] == expected
    else:
        assert delivered["hookSpecificOutput"]["additionalContext"] == expected
    assert "Invoice credits require an audit" in expected

    assert cli.main(["privacy", "--json", "--project", str(tmp_path)]) == 0
    enabled = json.loads(capsys.readouterr().out)["hosts"][host]
    assert enabled["capture"] == enabled["recall"] == "enabled"
    args = ["--agent", host, "--project", str(tmp_path)]
    assert cli.main(["service", "stop", *args]) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["capture"] == paused["recall"] == "paused"
    assert project.preview_context(tmp_path, "paused", "Invoice credits", agent=host) == ""
    assert json.loads(hook(tmp_path, host, "paused", "Invoice credits").stdout) == {}
    assert all(not session.endswith(":paused") for session, _, _ in rows(tmp_path))

    assert cli.main(["service", "start", *args]) == 0
    capsys.readouterr()
    hook(tmp_path, host, "resumed", "Resume on the new ledger.")
    assert any(session.endswith(":resumed") for session, _, _ in rows(tmp_path))
    assert cli.main(["reset", str(tmp_path), "--yes"]) == 0
    assert cli.main(["init", str(tmp_path)]) == 0
    assert json.loads(hook(tmp_path, host, "after-reset", "No capture while paused.").stdout) == {}
    assert rows(tmp_path) == []
    assert project.privacy_info(tmp_path)["hosts"][host]["capture"] == "paused"
    assert cli.main(["service", "start", *args]) == 0
    hook(tmp_path, host, "after-start", "Resume explicitly after reset.")
    assert len(rows(tmp_path)) == 2
    assert cli.main(["uninstall", *args]) == 0
    assert project.privacy_info(tmp_path)["hosts"][host]["recall"] == "not configured"
    assert json.loads(hook(tmp_path, host, "uninstalled", "No capture after uninstall.").stdout) == {}
    assert len(rows(tmp_path)) == 2


def test_concurrent_replay_and_abrupt_writer_interruption(tmp_path, capsys):
    install(tmp_path, "codex", capsys)
    install(tmp_path, "claude", capsys)
    child_code = """
import sys
from pathlib import Path
from memory import store, transcript
from trace_memory.native import capture_lock
from trace_memory.project import db_path
root = Path(sys.argv[1])
with capture_lock(root):
    connection = store.connect(db_path(root))
    connection.execute('BEGIN IMMEDIATE')
    transcript.record_message(connection, autocommit=False, session_id='interrupted', role='system', content='pending')
    print('ready', flush=True)
    sys.stdin.read()
"""
    with subprocess.Popen([sys.executable, "-c", child_code, str(tmp_path)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as owned_writer:
        try:
            assert owned_writer.stdout.readline().strip() == "ready"
        finally:
            owned_writer.terminate()
            owned_writer.wait(timeout=10)
    assert rows(tmp_path) == []
    with ThreadPoolExecutor(max_workers=4) as pool:
        calls = [pool.submit(hook, tmp_path, host, "replay", "Synthetic simultaneous replay")
                 for host in ("codex", "claude") for _ in range(2)]
        assert all("unavailable" not in call.result(timeout=25).stderr for call in calls)
    snapshot = rows(tmp_path)
    assert len(snapshot) == 4
    assert sorted((session, role) for session, role, _ in snapshot) == [
        ("claude:replay", "system"), ("claude:replay", "user"),
        ("codex:replay", "system"), ("codex:replay", "user"),
    ]
