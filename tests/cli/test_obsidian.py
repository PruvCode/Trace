"""Exercise the Obsidian export through the public TRACE command."""

import hashlib
import html
import re
import sqlite3
import subprocess
from contextlib import closing

from memory import episodic, store, task_library, transcript
from trace_memory import cli, project as project_mod


def test_obsidian_snapshot_refresh_and_privacy(tmp_path, capsys):
    project = tmp_path / "projects" / "Parcel Retry"
    project.mkdir(parents=True)
    vault = tmp_path / "vault"
    vault.mkdir()
    manual = vault / "Handoff.md"
    manual.write_text("# My notes\n", encoding="utf-8")
    source = project / "retry.py"
    source.write_text("def retry_delay():\n    return 0\n", encoding="utf-8")

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=TRACE fixture", "-c", "user.email=trace@example.invalid", *args],
            cwd=project, check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("init")
    git("add", "retry.py")
    git("-c", "commit.gpgsign=false", "commit", "-m", "Synthetic retry fixture")
    commit = git("rev-parse", "HEAD")
    project_mod.init_project(project)
    db = project_mod.db_path(project)
    secret = "ghp_" + "Ab9Z" * 9
    with closing(store.connect(db)) as conn:
        task = task_library.create_task(conn, "Retry policy", "Delivery")
        session = "session-fixture"
        transcript.record_message(conn, session_id=session, role="system", source_id=f"session:{session}",
                                  content="Session began.")
        task_library.bind_session(conn, task["id"], session)
        message = transcript.record_message(conn, session_id=session, role="assistant",
                                            content="PRIVATE_TRANSCRIPT_CANARY")
        old = episodic.record_event(conn, type="decision", repo="parcel", task_id=task["id"],
                                    payload={"decision": "Use the old retry rule", "status": "hypothesis"})
        current = episodic.record_event(
            conn, type="decision", repo="parcel", task_id=task["id"],
            payload={"decision": "Use a 47-second delay", "status": "verified", "supersedes": old},
            evidence=[{"type": "transcript", "message_id": message},
                      {"type": "git", "commit": commit, "path": "retry.py"}], project_root=project,
        )
        tool = episodic.record_event(
            conn, type="observation", repo="parcel", source="capture", task_id=task["id"],
            payload={"kind": "test_outcome", "outcome": "failed", "counts": {"failed": 1},
                     "message": "PRIVATE_TEST_COMMAND_CANARY"},
        )
        episodic.record_event(conn, type="observation", repo="parcel", source="capture",
                              payload={"message": "PRIVATE_GENERIC_TOOL_CANARY"})
        activity = episodic.record_event(conn, type="observation", repo="parcel",
                                         payload={"message": "safe placeholder"})
        with conn:
            conn.execute("UPDATE events SET payload=? WHERE id=?", (
                '{"message": "Legacy ' + secret + ' ![[private]] ![badge](https://example.test/pixel)"}', activity,
            ))
        refs = conn.execute("SELECT reference FROM event_evidence WHERE event_id=? ORDER BY id", (current,)).fetchall()

    def export():
        return cli.main(["obsidian", "--project", str(project), "--vault", str(vault)])

    before = hashlib.sha256(db.read_bytes()).digest()
    assert export() == 0
    note, = vault.glob("TRACE *.md")
    text = note.read_text(encoding="utf-8")
    assert before == hashlib.sha256(db.read_bytes()).digest()
    visible = html.unescape(text)
    assert all(value in visible for value in (
        "Retry policy", "Delivery", f"#### Event {tool}", "pytest failed", "1 failed", "47-second delay",
        f"Superseded** by [[#Event {current}]]", "Synthetic retry fixture", "retry_delay", session, "· current",
        *(f"trace evidence {ref[0]}" for ref in refs),
    ))
    assert "REDACTED" in text
    assert all(value not in text for value in (secret, "PRIVATE_", "![[private]]", "https://example.test/pixel",
                                              str(project), str(vault)))
    targets = re.findall(r"\[\[#([^]|]+)(?:\|[^]]*)?]]", text)
    headings = set(re.findall(r"^#{1,6} (.+)$", text, flags=re.M))
    blocks = set(re.findall(r" (\^trace-task-[0-9a-f]{32})$", text, flags=re.M))
    assert targets and blocks and set(targets) <= headings | blocks

    source.write_text("def retry_delay():\n    return 99\n", encoding="utf-8")
    assert export() == 0
    refreshed = note.read_text(encoding="utf-8")
    assert "· stale" in refreshed and len(list(vault.glob("TRACE *.md"))) == 1
    note.write_text(refreshed + "\n# Hand-edited note\n", encoding="utf-8")
    edited = note.read_bytes()
    assert export() == 2
    assert "note was kept" in capsys.readouterr().err
    assert note.read_bytes() == edited
    assert manual.read_text(encoding="utf-8") == "# My notes\n"
    assert not list(vault.glob(".trace-*"))


def test_obsidian_empty_vault_and_legacy_database(tmp_path, capsys):
    project = tmp_path / "legacy"
    project.mkdir()
    db = project_mod.db_path(project)
    with closing(store.connect(db)) as conn:
        store.init_schema(conn)
        with conn:
            conn.execute("DROP TABLE task_sessions")
            conn.execute("DROP TABLE event_evidence")
            conn.execute("DROP INDEX idx_events_task_id")
            conn.execute("ALTER TABLE events DROP COLUMN task_id")
            conn.execute("DROP TABLE tasks")
            conn.execute("DROP TABLE test_capture_policy")
    missing = tmp_path / "missing-vault"
    args = ["obsidian", "--project", str(project), "--vault", str(missing)]
    assert cli.main(args) == 2 and not missing.exists()
    assert "vault folder was not found" in capsys.readouterr().err
    missing.mkdir()
    assert cli.main(args) == 0
    note, = missing.glob("TRACE *.md")
    assert "No tasks recorded." in note.read_text(encoding="utf-8")
    with closing(sqlite3.connect(db)) as conn:
        assert not store.has_task_schema(conn)
