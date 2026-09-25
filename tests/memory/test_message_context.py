"""Tests for production user-channel injection (Step 6).

Covers: current-session resolution/exclusion (A), prior inclusion (B),
fail-open ambiguity (C), project isolation (D), exactly-once shape and
idempotence (E), empty retrieval (F), bounds (G), malformed rows (H),
redaction (I), labeling (J), capture-contamination protection (K),
two/three-session continuity without echo (L/M), restart statelessness (N),
and independent projects (O). No network, no models; all databases are
temporary fakes or temp TRACE databases.
"""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from memory import message_context as mc_mod
from memory import session_context as sc_mod
from memory import store as store_mod
from memory import synthetic as synthetic_mod
from memory import transcript as transcript_mod
from memory import episodic as episodic_mod
from memory.capture import CaptureConfig


def _norm(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def _init_trace_db(path: Path) -> Path:
    conn = store_mod.connect(path)
    store_mod.init_schema(conn)
    conn.close()
    return path


def _add_message(db: Path, session_id: str, role: str, content: str,
                 timestamp: str | None = None):
    """Record a message; explicit timestamps keep session ordering deterministic.

    TRACE stamps second-precision times, so same-second inserts tie in
    newest-first ordering. Tests that depend on recency pass distinct
    timestamps explicitly.
    """
    conn = store_mod.connect(db)
    try:
        transcript_mod.record_message(
            conn, session_id=session_id, role=role, content=content,
            metadata={}, timestamp=timestamp,
        )
    finally:
        conn.close()


def _add_event(db: Path, kind: str, payload: dict):
    conn = store_mod.connect(db)
    try:
        episodic_mod.record_event(
            conn, type=kind, source="agent", repo="proj", payload=payload
        )
    finally:
        conn.close()


def _make_ocdb(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT NOT NULL, "
        "time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()
    return path


def _add_oc_session(ocdb: Path, session_id: str, directory: Path, age_ms: int,
                    updated_age_ms: int | None = None):
    """Insert a fake OpenCode session row.

    By default the session ended when created (``time_updated`` equals
    ``time_created``). Pass ``updated_age_ms=0`` for a still-live session.
    """
    now_ms = int(time.time() * 1000)
    if updated_age_ms is None:
        updated_age_ms = age_ms
    conn = sqlite3.connect(ocdb)
    try:
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated)"
            " VALUES (?, ?, ?, ?)",
            (session_id, _norm(directory), now_ms - age_ms, now_ms - updated_age_ms),
        )
        conn.commit()
    finally:
        conn.close()


def _trace_rows(db: Path, sql: str = "SELECT role, content FROM transcript_messages",
                params: tuple = ()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# A. Current-session exclusion ------------------------------------------------

def test_resolver_picks_newest_fresh_session(tmp_path):
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_oc_session(ocdb, "ses-old", proj, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-new", proj, age_ms=1_000)
    res = mc_mod.resolve_current_session(ocdb, proj)
    assert res == {"ok": True, "session_id": "ses-new", "reason": "ok"}


def test_assembly_excludes_current_includes_prior(tmp_path):
    trace_db = _init_trace_db(tmp_path / "memory.db")
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_message(trace_db, "ses-old", "user", "PRIOR-MARKER task")
    _add_message(trace_db, "ses-new", "user", "CURRENT-MARKER task")
    _add_oc_session(ocdb, "ses-old", proj, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-new", proj, age_ms=1_000)
    result = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=ocdb, project_dir=proj
    )
    assert result["stats"]["resolved"] is True
    assert result["stats"]["currentSession"] == "ses-new"
    assert "PRIOR-MARKER" in result["text"]
    assert "CURRENT-MARKER" not in result["text"]


# B. Prior-session inclusion ---------------------------------------------------

def test_prior_session_included(tmp_path):
    trace_db = _init_trace_db(tmp_path / "memory.db")
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_message(trace_db, "ses-old", "user", "Fix the login redirect bug")
    _add_message(trace_db, "ses-old", "assistant", "Fixed by reordering middleware")
    _add_oc_session(ocdb, "ses-old", proj, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-new", proj, age_ms=1_000)
    result = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=ocdb, project_dir=proj
    )
    assert "Fix the login redirect bug" in result["text"]
    assert "reordering middleware" in result["text"]


# C. Ambiguity fails open -------------------------------------------------------

def test_no_session_rows_fails_open(tmp_path):
    trace_db = _init_trace_db(tmp_path / "memory.db")
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_message(trace_db, "ses-old", "user", "PRIOR-MARKER task")
    res = mc_mod.resolve_current_session(ocdb, proj)
    assert res["ok"] is False and res["reason"] == "no-session"
    result = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=ocdb, project_dir=proj
    )
    assert result["items"] == [] and result["text"] == ""
    assert result["stats"]["resolved"] is False


def test_missing_opencode_db_fails_open(tmp_path):
    trace_db = _init_trace_db(tmp_path / "memory.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    res = mc_mod.resolve_current_session(tmp_path / "no-such.db", proj)
    assert res["ok"] is False and res["reason"] == "no-database"
    result = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=tmp_path / "no-such.db", project_dir=proj
    )
    assert result["items"] == [] and result["text"] == ""


def test_stale_newest_session_fails_open(tmp_path):
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_oc_session(ocdb, "ses-old", proj, age_ms=3_600_000)
    res = mc_mod.resolve_current_session(ocdb, proj)
    assert res["ok"] is False and res["reason"] == "stale-or-unknown"


def test_concurrent_sessions_fail_open(tmp_path):
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_oc_session(ocdb, "ses-a", proj, age_ms=2_000, updated_age_ms=0)
    _add_oc_session(ocdb, "ses-b", proj, age_ms=1_000)
    res = mc_mod.resolve_current_session(ocdb, proj)
    assert res["ok"] is False and res["reason"] == "concurrent"


def test_back_to_back_sequential_sessions_proceed(tmp_path):
    # The normal case the old creation-time rule got wrong: ses-old ended
    # before ses-new began, so ses-new unambiguously identifies the caller
    # even though both rows are fresh.
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_oc_session(ocdb, "ses-old", proj, age_ms=17_000)
    _add_oc_session(ocdb, "ses-new", proj, age_ms=1_000)
    res = mc_mod.resolve_current_session(ocdb, proj)
    assert res == {"ok": True, "session_id": "ses-new", "reason": "ok"}


def test_live_sibling_session_fails_open(tmp_path):
    # An older session that is still updating (TUI sibling, background run)
    # means newest-for-directory may not be the firing session.
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_oc_session(ocdb, "ses-old", proj, age_ms=3_600_000, updated_age_ms=0)
    _add_oc_session(ocdb, "ses-new", proj, age_ms=1_000)
    res = mc_mod.resolve_current_session(ocdb, proj)
    assert res["ok"] is False and res["reason"] == "concurrent"


# D. Project isolation -----------------------------------------------------------

def test_resolver_ignores_other_directories(tmp_path):
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    _add_oc_session(ocdb, "ses-b-fresh", proj_b, age_ms=1_000)
    _add_oc_session(ocdb, "ses-a-old", proj_a, age_ms=3_600_000)
    res = mc_mod.resolve_current_session(ocdb, proj_a)
    assert res["ok"] is False  # newest row for A itself is stale
    assert res["reason"] == "stale-or-unknown"


def test_assembly_project_isolation(tmp_path):
    db_a = _init_trace_db(tmp_path / "a.db")
    db_b = _init_trace_db(tmp_path / "b.db")
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj_a = tmp_path / "pa"
    proj_b = tmp_path / "pb"
    proj_a.mkdir()
    proj_b.mkdir()
    _add_message(db_a, "ses-a", "user", "ISOLATION-A-MARKER")
    _add_message(db_b, "ses-b", "user", "ISOLATION-B-MARKER")
    _add_oc_session(ocdb, "ses-a", proj_a, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-a-cur", proj_a, age_ms=1_000)
    _add_oc_session(ocdb, "ses-b", proj_b, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-b-cur", proj_b, age_ms=1_000)
    ra = mc_mod.get_message_start_context(db_a, opencode_db_path=ocdb, project_dir=proj_a)
    rb = mc_mod.get_message_start_context(db_b, opencode_db_path=ocdb, project_dir=proj_b)
    assert "ISOLATION-A-MARKER" in ra["text"]
    assert "ISOLATION-B-MARKER" not in ra["text"]
    assert "ISOLATION-B-MARKER" in rb["text"]
    assert "ISOLATION-A-MARKER" not in rb["text"]


# E. Exactly-once shape and idempotence ------------------------------------------

def test_plugin_registers_guarded_user_channel(tmp_path):
    from memory import opencode_plugin as plugin_mod
    from trace_memory import project as project_mod
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "main.py").write_text("x = 1\n", encoding="utf-8")
    project_mod.init_project(proj)
    plugin_mod.install_plugin(proj)
    source = (proj / ".opencode" / "plugins" / "trace-memory.js").read_text(encoding="utf-8")
    assert "experimental.chat.messages.transform" in source
    assert "experimental.chat.system.transform" not in source
    assert "traceMsgInjectedSessions" in source
    assert "output.messages.unshift" in source
    assert "synthetic" in source
    assert "stats.resolved === true" in source
    assert '"channel": "messages.transform"' in source or 'channel: "messages.transform"' in source


def test_repeated_assembly_is_identical(tmp_path):
    trace_db = _init_trace_db(tmp_path / "memory.db")
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_message(trace_db, "ses-old", "user", "repeatable task")
    _add_oc_session(ocdb, "ses-old", proj, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-new", proj, age_ms=1_000)
    first = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=ocdb, project_dir=proj
    )
    second = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=ocdb, project_dir=proj
    )
    assert first["text"] == second["text"]
    assert first["items"] == second["items"]


# F. Empty retrieval --------------------------------------------------------------

def test_empty_trace_db_injects_nothing(tmp_path):
    trace_db = _init_trace_db(tmp_path / "memory.db")
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_oc_session(ocdb, "ses-new", proj, age_ms=1_000)
    result = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=ocdb, project_dir=proj
    )
    assert result["stats"]["resolved"] is True
    assert result["items"] == [] and result["text"] == ""


# G. Bounds ------------------------------------------------------------------------

def test_message_path_bounds_are_deterministic(tmp_path):
    trace_db = _init_trace_db(tmp_path / "memory.db")
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    for n in range(10):
        _add_message(trace_db, "ses-big", "user", f"U{n}-" + "x" * 496)
    _add_oc_session(ocdb, "ses-big", proj, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-new", proj, age_ms=1_000)
    first = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=ocdb, project_dir=proj
    )
    second = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=ocdb, project_dir=proj
    )
    assert first["text"] == second["text"]
    assert sum(len(i["text"]) for i in first["items"]) <= sc_mod.MAX_CHARS
    assert len(first["items"]) <= sc_mod.MAX_ITEMS
    assert first["truncated"] is True


# H. Malformed rows -----------------------------------------------------------------

def test_malformed_rows_skipped_on_message_path(tmp_path):
    trace_db = _init_trace_db(tmp_path / "memory.db")
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_message(trace_db, "ses-ok", "user", "VALIDMARKER good content")
    conn = store_mod.connect(trace_db)
    try:
        conn.execute(
            "INSERT INTO transcript_messages (session_id, role, content, timestamp, metadata)"
            " VALUES (?, ?, ?, ?, ?)",
            ("ses-ok", "alien", "weird role row", "2026-01-01T00:00:00", "{}"),
        )
        conn.commit()
    finally:
        conn.close()
    _add_oc_session(ocdb, "ses-ok", proj, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-new", proj, age_ms=1_000)
    result = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=ocdb, project_dir=proj
    )
    assert "VALIDMARKER" in result["text"]
    assert "alien" not in result["text"]


# I. Secret redaction ------------------------------------------------------------------

def test_secrets_redacted_on_message_path(tmp_path):
    trace_db = _init_trace_db(tmp_path / "memory.db")
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_message(
        trace_db, "ses-sec", "user",
        "review config: password = hunter2-secret | postgres://admin:s3cretpw@db.internal:5432/app",
    )
    _add_oc_session(ocdb, "ses-sec", proj, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-new", proj, age_ms=1_000)
    result = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=ocdb, project_dir=proj
    )
    assert result["text"]
    assert "hunter2-secret" not in result["text"]
    assert "s3cretpw" not in result["text"]
    assert "REDACTED" in result["text"]


# J. Labeling ----------------------------------------------------------------------------

def test_synthetic_block_labeling():
    items = [
        {"kind": "message", "role": "user", "text": "hello"},
        {"kind": "finding", "role": "decision", "text": "use tabs"},
    ]
    text = synthetic_mod.render_user_block(items)
    assert text.startswith(synthetic_mod.SYNTHETIC_TITLE)
    assert text.rstrip().endswith(synthetic_mod.SYNTHETIC_FOOTER)
    assert "Previous session:" in text
    assert "<TRACE MEMORY>" not in text
    assert synthetic_mod.is_synthetic_memory_text(text) is True
    assert synthetic_mod.render_user_block([]) == ""
    assert synthetic_mod.is_synthetic_memory_text("ordinary user text") is False
    assert synthetic_mod.is_synthetic_memory_text(None) is False
    assert synthetic_mod.is_synthetic_memory_text(123) is False


# K. Capture-contamination protection ------------------------------------------------------

def _make_monitor(tmp_path, name="proj"):
    from memory.opencode_transparent import OpenCodeDatabaseMonitor
    ocdb = tmp_path / f"{name}-oc.db"
    ocdb.touch()
    trace_db = _init_trace_db(tmp_path / f"{name}-trace.db")
    monitor = OpenCodeDatabaseMonitor(
        opencode_db_path=ocdb,
        trace_db_path=trace_db,
        project_name=name,
        config=CaptureConfig(),
    )
    return monitor, trace_db


def _make_service(tmp_path, name="svc"):
    from memory.opencode_service import OpenCodeCaptureService
    from memory.opencode_service import CaptureConfig as ServiceConfig
    project = tmp_path / name
    project.mkdir(exist_ok=True)
    ocdb = tmp_path / f"{name}-oc.db"
    ocdb.touch()
    trace_db = _init_trace_db(tmp_path / f"{name}-trace.db")
    service = OpenCodeCaptureService(
        project_path=project,
        opencode_db_path=ocdb,
        trace_db_path=trace_db,
        project_name=name,
        config=ServiceConfig(),
    )
    return service, trace_db


def test_monitor_drops_synthetic_message_rows(tmp_path):
    monitor, trace_db = _make_monitor(tmp_path, name="m1")
    monitor._process_message({
        "id": "msg-syn",
        "session_id": "ses-1",
        "data": json.dumps({"role": "user", "content": synthetic_mod.SYNTHETIC_TITLE + "\nbody"}),
    })
    assert _trace_rows(trace_db, "SELECT COUNT(*) FROM transcript_messages")[0][0] == 0
    monitor._process_message({
        "id": "msg-real",
        "session_id": "ses-1",
        "data": json.dumps({"role": "user", "content": "genuine user question"}),
    })
    rows = _trace_rows(trace_db)
    assert len(rows) == 1 and rows[0][1] == "genuine user question"


def test_monitor_drops_synthetic_text_parts(tmp_path):
    monitor, trace_db = _make_monitor(tmp_path, name="m2")
    monitor._process_part({
        "id": "part-syn",
        "message_id": "msg-1",
        "session_id": "ses-1",
        "data": json.dumps({"type": "text", "synthetic": True,
                             "text": synthetic_mod.SYNTHETIC_TITLE + "\nbody"}),
    })
    assert _trace_rows(trace_db, "SELECT COUNT(*) FROM transcript_messages")[0][0] == 0
    # A synthetic-flagged part with ordinary content still flows (no behavior change).
    monitor._process_part({
        "id": "part-task",
        "message_id": "msg-1",
        "session_id": "ses-1",
        "data": json.dumps({"type": "text", "synthetic": True, "text": "call the task tool"}),
    })
    rows = _trace_rows(trace_db)
    assert len(rows) == 1 and rows[0][1] == "call the task tool"


def test_service_drops_synthetic_content(tmp_path):
    service, trace_db = _make_service(tmp_path, name="s1")
    service._process_message({
        "id": "msg-syn",
        "session_id": "ses-1",
        "data": json.dumps({"role": "user", "content": synthetic_mod.SYNTHETIC_TITLE + "\nbody"}),
    })
    service._process_part({
        "id": "part-syn",
        "message_id": "msg-1",
        "session_id": "ses-1",
        "data": json.dumps({"type": "text", "text": synthetic_mod.SYNTHETIC_TITLE + "\nbody"}),
    })
    assert _trace_rows(trace_db, "SELECT COUNT(*) FROM transcript_messages")[0][0] == 0
    service._process_part({
        "id": "part-real",
        "message_id": "msg-1",
        "session_id": "ses-1",
        "data": json.dumps({"type": "text", "text": "ordinary assistant reply"}),
    })
    assert _trace_rows(trace_db, "SELECT COUNT(*) FROM transcript_messages")[0][0] == 1


def test_retrieval_skips_planted_synthetic_rows(tmp_path):
    trace_db = _init_trace_db(tmp_path / "memory.db")
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_message(trace_db, "ses-old", "user", "REALMARKER genuine work",
                 timestamp="2026-01-01T00:00:01")
    conn = store_mod.connect(trace_db)
    try:
        transcript_mod.record_message(
            conn, session_id="ses-old", role="user",
            content=synthetic_mod.SYNTHETIC_TITLE + "\nplanted echo", metadata={},
            timestamp="2026-01-01T00:00:02",
        )
    finally:
        conn.close()
    _add_oc_session(ocdb, "ses-old", proj, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-new", proj, age_ms=1_000)
    result = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=ocdb, project_dir=proj
    )
    assert "REALMARKER" in result["text"]
    # The planted echo must not be re-surfaced (the framing title itself is
    # expected: every injected block carries it by design).
    assert "planted echo" not in result["text"]


# L/M. Continuity without echo ---------------------------------------------------------------

def test_two_session_continuity(tmp_path):
    trace_db = _init_trace_db(tmp_path / "memory.db")
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_message(trace_db, "ses-1", "user", "CONT-A-MARKER first findings")
    _add_oc_session(ocdb, "ses-1", proj, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-2", proj, age_ms=1_000)
    result = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=ocdb, project_dir=proj
    )
    assert result["stats"]["currentSession"] == "ses-2"
    assert "CONT-A-MARKER" in result["text"]


def test_three_session_continuity_without_echo(tmp_path):
    trace_db = _init_trace_db(tmp_path / "memory.db")
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_message(trace_db, "ses-1", "user", "CONT-A-MARKER first findings",
                 timestamp="2026-01-01T00:00:01")
    _add_message(trace_db, "ses-2", "user", "CONT-B-MARKER second findings",
                 timestamp="2026-01-01T00:00:02")
    conn = store_mod.connect(trace_db)
    try:
        transcript_mod.record_message(
            conn, session_id="ses-2", role="user",
            content=synthetic_mod.SYNTHETIC_TITLE + "\nworst-case echo", metadata={},
            timestamp="2026-01-01T00:00:03",
        )
    finally:
        conn.close()
    _add_oc_session(ocdb, "ses-1", proj, age_ms=7_200_000)
    _add_oc_session(ocdb, "ses-2", proj, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-3", proj, age_ms=1_000)
    result = mc_mod.get_message_start_context(
        trace_db, opencode_db_path=ocdb, project_dir=proj
    )
    assert result["stats"]["currentSession"] == "ses-3"
    # Most-recent-prior policy surfaces ses-2 only; the planted echo is skipped.
    assert "CONT-B-MARKER" in result["text"]
    assert "CONT-A-MARKER" not in result["text"]
    assert "worst-case echo" not in result["text"]


# N. Restart statelessness -----------------------------------------------------------------------

def test_resolver_stateless_across_calls(tmp_path):
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj = tmp_path / "proj"
    proj.mkdir()
    _add_oc_session(ocdb, "ses-old", proj, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-new", proj, age_ms=1_000)
    first = mc_mod.resolve_current_session(ocdb, proj)
    second = mc_mod.resolve_current_session(ocdb, proj)
    assert first == second == {"ok": True, "session_id": "ses-new", "reason": "ok"}


# O. Independent projects -----------------------------------------------------------------------------

def test_projects_resolve_and_retrieve_independently(tmp_path):
    ocdb = _make_ocdb(tmp_path / "oc.db")
    proj_a = tmp_path / "pa"
    proj_b = tmp_path / "pb"
    proj_a.mkdir()
    proj_b.mkdir()
    _add_oc_session(ocdb, "ses-a-old", proj_a, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-a-new", proj_a, age_ms=1_000)
    _add_oc_session(ocdb, "ses-b-old", proj_b, age_ms=3_600_000)
    _add_oc_session(ocdb, "ses-b-new", proj_b, age_ms=1_000)
    ra = mc_mod.resolve_current_session(ocdb, proj_a)
    rb = mc_mod.resolve_current_session(ocdb, proj_b)
    assert ra["session_id"] == "ses-a-new"
    assert rb["session_id"] == "ses-b-new"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
