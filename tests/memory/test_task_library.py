"""Task scoping and provenance against disposable databases and Git fixtures."""

import sqlite3
import subprocess
from contextlib import closing

import pytest

from memory import episodic, session_context, store, task_library, transcript


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "memory.db"
    with closing(store.connect(path)) as conn:
        store.init_schema(conn)
        yield conn, path


def test_scoped_fts_filters_before_limit_and_startup_keeps_general_findings(database):
    conn, path = database
    auth = task_library.create_task(conn, "Login timeout", "auth")["id"]
    sync = task_library.create_task(conn, "Sync timeout", "sync")["id"]
    legacy = episodic.record_event(conn, type="investigation", repo="demo", payload={"message": "general fact"})
    wanted = episodic.record_event(
        conn, type="attempt", repo="demo", task_id=auth, payload={"message": "timeout auth fix failed"},
    )
    for _ in range(store.MAX_LIMIT + 1):
        episodic.record_event(
            conn, type="attempt", repo="demo", task_id=sync, payload={"message": "timeout timeout"},
        )
    assert [row.id for row in episodic.search_events(conn, query="timeout", task_id=auth, limit=1)] == [wanted]
    assert "task_id" not in episodic.search_events(conn, limit=1)[0].as_record()
    assert task_library.link_event(conn, auth, legacy) == task_library.link_event(conn, auth, legacy)
    with pytest.raises(ValueError, match="another task"):
        task_library.link_event(conn, sync, legacy)
    with pytest.raises(ValueError, match="same task scope"):
        episodic.record_event(conn, type="decision", repo="demo", task_id=sync, payload={"supersedes": wanted})
    newer = episodic.record_event(
        conn, type="decision", repo="demo", task_id=auth, payload={"supersedes": wanted},
    )
    old = episodic.search_events(conn, task_id=auth, type="attempt")[0]
    assert old.superseded_by == newer
    general = episodic.record_event(conn, type="observation", repo="demo", payload={"message": "general finding"})
    items = session_context.get_session_start_context(path)["items"]
    assert [item["text"] for item in items] == ["general finding"]
    assert episodic.search_events(conn, type="observation")[0].id == general


def test_evidence_and_fts_roll_back_together_inside_capture_transaction(database):
    conn, _ = database
    task_id = task_library.create_task(conn, "Atomic write")["id"]
    message_id = transcript.record_message(conn, session_id="demo", role="user", content="Synthetic evidence")
    evidence = [{"type": "transcript", "message_id": message_id}]
    conn.execute(
        "CREATE TRIGGER reject_evidence BEFORE INSERT ON event_evidence"
        " BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END"
    )
    earlier = episodic.record_event(conn, autocommit=False, type="observation", repo="demo")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic write failure"):
        episodic.record_event(
            conn, autocommit=False, type="attempt", repo="demo", task_id=task_id,
            payload={"message": "rollback marker"}, evidence=evidence,
        )
    assert conn.in_transaction
    conn.commit()
    assert [row.id for row in episodic.search_events(conn)] == [earlier]
    assert conn.execute("SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'rollback'").fetchone()[0] == 0
    conn.execute("DROP TRIGGER reject_evidence")
    options = dict(
        type="attempt", repo="demo", task_id=task_id, source_id="fixture:1", evidence=evidence * 2,
    )
    assert episodic.record_event(conn, **options) == episodic.record_event(conn, **options)
    assert episodic.search_events(conn, task_id=task_id)[0].evidence == evidence
    store.clear_events(conn)
    assert conn.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0] == 0
    assert transcript.get_session_messages(conn, "demo")[0].id == message_id


def test_evidence_is_bound_to_local_sources(database, tmp_path, monkeypatch):
    conn, _ = database
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    file = root / "test file.py"
    file.write_text("answer = 42\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", file.name], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Trace Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=missing-hooks", "commit", "-qm", "test"],
        cwd=root, check=True,
    )
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    ref = {"type": "git", "commit": commit, "path": file.name}
    task_id = task_library.create_task(conn, "Local provenance")["id"]
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=foreign_root, check=True)
    with closing(store.connect(foreign_root / "memory.db")) as other:
        store.init_schema(other)
        foreign_message = transcript.record_message(other, session_id="other", role="user", content="foreign")
        foreign_task = task_library.create_task(other, "Other project")["id"]
        with pytest.raises(ValueError, match="does not exist in this project"):
            episodic.search_events(conn, task_id=foreign_task)
        with pytest.raises(ValueError, match="transcript message ID does not exist"):
            episodic.record_event(conn, type="attempt", repo="demo",
                                  evidence=[{"type": "transcript", "message_id": foreign_message}])
    monkeypatch.setenv("GIT_DIR", str(root / ".git"))
    with pytest.raises(ValueError, match="does not exist at that commit"):
        episodic.record_event(conn, type="attempt", repo="demo", project_root=foreign_root, evidence=[ref])
    for bad in [dict(ref, path="../outside.py"), dict(ref, commit="HEAD"), dict(ref, path="missing.py")]:
        with pytest.raises(ValueError, match="Git evidence"):
            episodic.record_event(conn, type="attempt", repo="demo", project_root=root, evidence=[bad])
    assert episodic.search_events(conn) == []
    episodic.record_event(conn, type="attempt", repo="demo", task_id=task_id, project_root=root, evidence=[ref])
    assert episodic.search_events(conn, task_id=task_id)[0].evidence == [ref]
