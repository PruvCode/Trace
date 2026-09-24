"""Unit/integration tests for bounded session-start retrieval (Step 5.2).

Fast, deterministic, no network: builds TRACE databases directly and
exercises memory.session_context assembly policy.
"""

import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from memory import session_context as sc_mod
from memory import store as store_mod
from memory import transcript as transcript_mod
from memory import episodic as episodic_mod


@pytest.fixture()
def trace_db():
    """Empty initialized TRACE database in a temp dir."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "memory.db"
        conn = store_mod.connect(db)
        store_mod.init_schema(conn)
        conn.close()
        yield db


def _add_message(db: Path, session_id: str, role: str, content: str):
    conn = store_mod.connect(db)
    try:
        transcript_mod.record_message(
            conn, session_id=session_id, role=role, content=content, metadata={}
        )
    finally:
        conn.close()


def _add_event(db: Path, kind: str, payload: dict, repo: str = "proj"):
    conn = store_mod.connect(db)
    try:
        episodic_mod.record_event(
            conn, type=kind, source="agent", repo=repo, payload=payload
        )
    finally:
        conn.close()


def test_no_prior_memory_injects_nothing(trace_db):
    result = sc_mod.get_session_start_context(trace_db)
    assert result["items"] == []
    assert result["text"] == ""
    assert result["truncated"] is False


def test_one_previous_session_tail_injected(trace_db):
    _add_message(trace_db, "ses-old", "user", "Fix the login redirect bug")
    _add_message(trace_db, "ses-old", "assistant", "Fixed by reordering middleware")
    started = time.perf_counter()
    result = sc_mod.get_session_start_context(trace_db)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert elapsed_ms < 2000, f"assembly too slow: {elapsed_ms:.1f}ms"
    assert "<TRACE MEMORY>" in result["text"]
    assert "Fix the login redirect bug" in result["text"]
    assert "reordering middleware" in result["text"]
    roles = [i["role"] for i in result["items"]]
    assert "user" in roles and "assistant" in roles


def test_only_most_recent_session_selected(trace_db):
    _add_message(trace_db, "ses-older", "user", "OLDMARKER ancient task")
    _add_message(trace_db, "ses-newer", "user", "NEWMARKER current task")
    result = sc_mod.get_session_start_context(trace_db)
    assert "NEWMARKER" in result["text"]
    assert "OLDMARKER" not in result["text"]


def test_current_session_excluded(trace_db):
    _add_message(trace_db, "ses-prior", "user", "PRIORMARKER earlier work")
    _add_message(trace_db, "ses-current", "user", "CURRENTMARKER just started")
    result = sc_mod.get_session_start_context(
        trace_db, exclude_session_id="ses-current"
    )
    assert "PRIORMARKER" in result["text"]
    assert "CURRENTMARKER" not in result["text"]


def test_recent_findings_bounded(trace_db):
    for n in range(7):
        _add_event(trace_db, "observation", {"finding": f"FINDING-{n}"})
    result = sc_mod.get_session_start_context(trace_db)
    findings = [i for i in result["items"] if i["kind"] == "finding"]
    assert len(findings) == 5
    texts = " ".join(i["text"] for i in findings)
    assert "FINDING-6" in texts  # newest kept
    assert "FINDING-0" not in texts  # oldest dropped
    assert "FINDING-1" not in texts


def test_total_chars_bounded_deterministically(trace_db):
    # 10 tail messages x 500 chars (per-item cap) = 5000 > 4000 budget.
    for n in range(10):
        _add_message(trace_db, "ses-big", "user", f"U{n}-" + "x" * 496)
    first = sc_mod.get_session_start_context(trace_db)
    second = sc_mod.get_session_start_context(trace_db)
    assert first["text"] == second["text"]
    total = sum(len(i["text"]) for i in first["items"])
    assert total <= sc_mod.MAX_CHARS
    assert first["truncated"] is True


def test_item_budget_enforced():
    items = [{"kind": "message", "role": "user", "text": f"m{n}"} for n in range(25)]
    kept, truncated = sc_mod._apply_budgets(list(items))
    assert len(kept) == sc_mod.MAX_ITEMS
    assert truncated is True
    assert [i["text"] for i in kept] == [f"m{n}" for n in range(20)]


def test_malformed_rows_skipped(trace_db):
    _add_message(trace_db, "ses-ok", "user", "VALIDMARKER good content")
    conn = store_mod.connect(trace_db)
    try:
        conn.execute(
            "INSERT INTO transcript_messages (session_id, role, content, timestamp, metadata)"
            " VALUES (?, ?, ?, ?, ?)",
            ("ses-ok", "alien", "weird role row", "2026-01-01T00:00:00", "{}"),
        )
        conn.execute(
            "INSERT INTO transcript_messages (session_id, role, content, timestamp, metadata)"
            " VALUES (?, ?, ?, ?, ?)",
            ("ses-ok", "user", "", "2026-01-01T00:00:00", "{}"),
        )
        conn.commit()
    finally:
        conn.close()
    result = sc_mod.get_session_start_context(trace_db)
    assert "VALIDMARKER" in result["text"]
    assert "alien" not in result["text"]


def test_retrieval_error_fails_open(tmp_path):
    missing = tmp_path / "no-such.db"
    result = sc_mod.get_session_start_context(missing)
    assert result == {"items": [], "text": "", "truncated": False, "stats": result["stats"]}
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"\x00\x01not a database")
    result = sc_mod.get_session_start_context(garbage)
    assert result["items"] == [] and result["text"] == ""


def test_secrets_redacted_before_injection(trace_db):
    secrets = [
        "api_key = sk-abcdefghij1234567890ABCDEFGHIJKLMNOP",
        "bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dummy-signature-value-here",
        "password = hunter2-secret",
        "postgres://admin:s3cretpw@db.internal:5432/app",
        "-----BEGIN PRIVATE KEY-----\nMIIBvTBX\n-----END PRIVATE KEY-----",
    ]
    _add_message(trace_db, "ses-sec", "user", "review config: " + " | ".join(secrets))
    result = sc_mod.get_session_start_context(trace_db)
    assert result["text"], "expected redacted context, got nothing"
    for raw in ["hunter2-secret", "s3cretpw", "MIIBvTBX", "dummy-signature"]:
        assert raw not in result["text"], f"secret leaked: {raw[:12]}..."
    assert "REDACTED" in result["text"]


def test_project_isolation(tmp_path):
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    for db in (db_a, db_b):
        conn = store_mod.connect(db)
        store_mod.init_schema(conn)
        conn.close()
    _add_message(db_a, "ses-a", "user", "ISOLATION-A-MARKER")
    _add_message(db_b, "ses-b", "user", "ISOLATION-B-MARKER")
    ra = sc_mod.get_session_start_context(db_a)
    rb = sc_mod.get_session_start_context(db_b)
    assert "ISOLATION-A-MARKER" in ra["text"]
    assert "ISOLATION-B-MARKER" not in ra["text"]
    assert "ISOLATION-B-MARKER" in rb["text"]
    assert "ISOLATION-A-MARKER" not in rb["text"]


def test_repeated_assembly_is_deterministic(trace_db):
    _add_message(trace_db, "ses-rep", "user", "repeatable task")
    _add_message(trace_db, "ses-rep", "assistant", "repeatable answer")
    _add_event(trace_db, "decision", {"decision": "use tabs"})
    first = sc_mod.get_session_start_context(trace_db)
    second = sc_mod.get_session_start_context(trace_db, exclude_session_id="other")
    assert first["text"] == second["text"]
    assert first["items"] == second["items"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
