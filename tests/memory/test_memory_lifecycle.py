"""Stale and superseded memory: what a new session is told is current.

Minimal lifecycle on top of episodic events (no schema change):
payload "status" (hypothesis / observed / verified) and "supersedes"
(earlier event ids). Superseded and possibly-stale are derived.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from memory import episodic as episodic_mod
from memory import session_context as sc_mod
from memory import store as store_mod


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("MIDDLEWARE = []\n", encoding="utf-8")
    (root / "app" / "other.py").write_text("X = 1\n", encoding="utf-8")
    db = root / ".agent-memory" / "memory.db"
    conn = store_mod.connect(db)
    store_mod.init_schema(conn)
    conn.close()
    return root, db


def _record(db: Path, **fields) -> int:
    conn = store_mod.connect(db)
    try:
        return episodic_mod.record_event(conn, source="agent", repo="proj", **fields)
    finally:
        conn.close()


def _findings(db: Path) -> list[dict]:
    return [i for i in sc_mod.get_session_start_context(db)["items"] if i["kind"] == "finding"]


def _age(path: Path, seconds: float) -> None:
    """Pretend a file was last written ``seconds`` ago."""
    when = time.time() - seconds
    os.utime(path, (when, when))


def test_hypothesis_is_not_presented_as_verified(project):
    root, db = project
    _record(db, type="investigation", payload={"finding": "token expiry may be off by one", "status": "hypothesis"})
    _record(db, type="decision", payload={"decision": "keep idle limit at 15 min", "status": "verified"})
    roles = {f["text"]: f["role"] for f in _findings(db)}
    assert roles["token expiry may be off by one"] == "investigation (hypothesis, unverified)"
    assert roles["keep idle limit at 15 min"] == "decision (verified)"


def test_newer_verified_finding_supersedes_contradiction(project):
    root, db = project
    old = _record(db, type="decision", payload={"decision": "fix goes in refresh_token()"})
    _record(db, type="decision", payload={"decision": "fix goes in middleware order", "status": "verified",
                                          "supersedes": [old]})
    texts = [f["text"] for f in _findings(db)]
    assert texts == ["fix goes in middleware order"], "superseded conclusion injected as current"


def test_superseded_evidence_stays_inspectable(project):
    root, db = project
    old = _record(db, type="attempt", payload={"action": "raise SESSION_TTL to 3600"})
    new = _record(db, type="decision", payload={"decision": "reverted TTL change", "supersedes": old})
    conn = store_mod.connect(db)
    events = {e.id: e for e in episodic_mod.search_events(conn)}
    hits = episodic_mod.search_events(conn, query="SESSION_TTL")
    conn.close()
    assert events[old].superseded_by == new and events[new].superseded_by is None
    assert events[old].payload == {"action": "raise SESSION_TTL to 3600"}  # original record unchanged
    assert [e.id for e in hits] == [old]


def test_unrelated_change_or_commit_does_not_mark_findings_stale(project):
    root, db = project
    for path in (root / "app" / "main.py", root / "app" / "other.py"):
        _age(path, 3600)
    _record(db, type="decision", file="app/main.py", payload={"decision": "middleware order fix"})
    _record(db, type="observation", payload={"finding": "no file link"})
    (root / "app" / "other.py").write_text("X = 2\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "app/other.py"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "unrelated"],
                   cwd=root, check=True)
    assert all("may be stale" not in f["role"] for f in _findings(db))


def test_relevant_change_triggers_staleness_warning(project):
    root, db = project
    main = root / "app" / "main.py"
    _age(main, 3600)
    _record(db, type="decision", file="app/main.py", payload={"decision": "middleware order fix"})
    assert "may be stale" not in _findings(db)[0]["role"]
    main.write_text("MIDDLEWARE = ['changed']\n", encoding="utf-8")
    os.utime(main, (time.time() + 5, time.time() + 5))
    assert _findings(db)[0]["role"] == "decision (may be stale: app/main.py changed after this was recorded)"
    main.unlink()
    assert _findings(db)[0]["role"] == "decision (may be stale: app/main.py no longer exists)"


def test_supersession_is_project_scoped_and_validated(project, tmp_path):
    root, db = project
    other_db = tmp_path / "other" / ".agent-memory" / "memory.db"
    conn = store_mod.connect(other_db)
    store_mod.init_schema(conn)
    conn.close()
    target = _record(db, type="decision", payload={"decision": "project A conclusion"})
    with pytest.raises(ValueError, match="unknown event id"):
        _record(other_db, type="decision", payload={"decision": "B", "supersedes": [target + 50]})
    with pytest.raises(ValueError, match="status"):
        _record(db, type="decision", payload={"decision": "x", "status": "certain"})
    with pytest.raises(ValueError, match="supersedes"):
        _record(db, type="decision", payload={"decision": "x", "supersedes": ["1"]})
    assert [f["text"] for f in _findings(db)] == ["project A conclusion"]


def test_historical_events_remain_readable(tmp_path):
    """Events written before the lifecycle existed (old schema) still read and inject."""
    root = tmp_path / "legacy"
    db = root / ".agent-memory" / "memory.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE events (id INTEGER PRIMARY KEY, type TEXT NOT NULL, timestamp TEXT NOT NULL,
            source TEXT NOT NULL, repo TEXT NOT NULL, commit_sha TEXT, file TEXT, symbol TEXT,
            payload TEXT NOT NULL DEFAULT '{}');
        CREATE VIRTUAL TABLE events_fts USING fts5(type, symbol, file, payload_text);
        INSERT INTO events (type, timestamp, source, repo, payload)
            VALUES ('decision', '2026-02-01T00:00:00+00:00', 'agent', 'r', '{"decision": "legacy decision"}');
        INSERT INTO events_fts (rowid, type, symbol, file, payload_text)
            VALUES (1, 'decision', '', '', '{"decision": "legacy decision"}');
    """)
    conn.commit()
    conn.close()
    conn = store_mod.connect(db)
    store_mod.init_schema(conn)
    events = episodic_mod.search_events(conn, query="legacy")
    conn.close()
    assert len(events) == 1 and events[0].superseded_by is None
    assert [(f["role"], f["text"]) for f in _findings(db)] == [("decision", "legacy decision")]


def test_cli_records_status_and_supersession(project, capsys):
    from trace_memory import cli

    root, db = project
    assert cli.main(["record", str(root), "--type", "decision", "--message", "first idea"]) == 0
    first = __import__("json").loads(capsys.readouterr().out)["id"]
    assert cli.main(["record", str(root), "--type", "decision", "--message", "checked idea",
                     "--status", "verified", "--supersedes", str(first)]) == 0
    assert [f["role"] for f in _findings(db)] == ["decision (verified)"]
