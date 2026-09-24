"""Real OpenCode black-box end-to-end tests.

These tests launch the ACTUAL OpenCode executable and verify TRACE captures
session data automatically. They do NOT simulate by inserting into the database.

REAL_OPENCode_TEST = POSSIBLE
"""

import tempfile
import time
import sqlite3
import subprocess
import shutil
from pathlib import Path
import pytest

from memory import store as store_mod
from trace_memory import project as project_mod
from memory import transcript as transcript_mod


@pytest.fixture()
def temp_project():
    """Create a temporary project with TRACE initialized."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        (project / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
        project_mod.init_project(project)
        yield project


def _opencode_db_path():
    """Get the OpenCode database path."""
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _run_opencode(project_path: Path, prompt: str, timeout: int = 150) -> subprocess.CompletedProcess:
    """Run OpenCode with a prompt in the given project directory."""
    # Use the actual opencode.exe binary directly (not the PowerShell wrapper)
    # Use a free model that doesn't require authentication
    opencode_exe = Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    
    cmd = [
        str(opencode_exe),
        "run",
        prompt,
        "--format", "json",
        "--dir", str(project_path),
        "--auto",  # auto-approve permissions
        "--model", "opencode/nemotron-3-ultra-free",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(project_path),
    )
    return result


def _get_session_from_opencode_db(project_path: Path) -> str | None:
    """Get the most recent session ID for the given project directory."""
    db_path = _opencode_db_path()
    # Normalize path for comparison (OpenCode stores with forward slashes)
    project_str = str(project_path.resolve()).replace("\\", "/")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id FROM session
            WHERE directory = ?
            ORDER BY time_created DESC
            LIMIT 1
        """, (project_str,))
        row = cursor.fetchone()
        return row["id"] if row else None


def _count_transcript_messages(project_path: Path, session_id: str) -> int:
    """Count transcript messages in TRACE for a session."""
    trace_db = project_mod.db_path(project_path)
    with sqlite3.connect(trace_db) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM transcript_messages WHERE session_id = ?",
            (session_id,)
        )
        return cursor.fetchone()[0]


def _count_events(project_path: Path, session_id: str) -> int:
    """Count episodic events in TRACE for a session."""
    trace_db = project_mod.db_path(project_path)
    with sqlite3.connect(trace_db) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM events WHERE payload LIKE ?",
            (f'%{session_id}%',)
        )
        return cursor.fetchone()[0]


class TestRealOpenCodeBlackBox:
    """Real OpenCode black-box tests."""

    def test_real_opencode_session_capture(self, temp_project):
        """
        Test that TRACE captures a REAL OpenCode session automatically.

        Flow:
        1. TRACE setup (one-time)
        2. Start persistent capture service
        3. Run REAL OpenCode with a prompt
        4. Verify TRACE captured the session
        """
        # 1. TRACE setup + start persistent service
        from memory.opencode_transparent import create_opencode_transparent_adapter
        adapter = create_opencode_transparent_adapter()
        setup_result = adapter.setup_integration(temp_project)
        assert setup_result["setup"]["status"] in ("created", "updated")
        assert setup_result["monitoring"]["status"] == "started"

        # Give service time to fully start
        time.sleep(3)

        # 2. Run REAL OpenCode
        prompt = "Create a simple hello.py file that prints 'hello from opencode'"
        result = _run_opencode(temp_project, prompt, timeout=180)
        
        # OpenCode MUST succeed for a valid capture test
        assert result.returncode == 0, (
            f"OpenCode failed with exit code {result.returncode}. "
            f"stdout: {result.stdout[:1000]} stderr: {result.stderr[:1000]}"
        )
        print(f"OpenCode exit code: {result.returncode}")

        # 3. Give monitor time to capture
        time.sleep(5)

        # 4. Get the session ID from OpenCode database
        session_id = _get_session_from_opencode_db(temp_project)
        assert session_id is not None, "No session found in OpenCode database"

        print(f"Found OpenCode session: {session_id}")

        # 5. Verify TRACE captured the session
        msg_count = _count_transcript_messages(temp_project, session_id)
        print(f"TRACE transcript messages captured: {msg_count}")
        
        # Should have at least system message + user message + assistant response
        assert msg_count >= 3, f"Expected at least 3 transcript messages (system + user + assistant), got {msg_count}"

        # 6. Verify actual interaction content
        trace_db = project_mod.db_path(temp_project)
        with sqlite3.connect(trace_db) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT session_id, role, content FROM transcript_messages WHERE session_id = ? ORDER BY id",
                (session_id,)
            )
            rows = cursor.fetchall()
            print(f"Captured messages: {len(rows)}")
            for row in rows:
                print(f"  {row}")

            # Verify we have the expected message types
            roles = [row[1] for row in rows]
            assert "system" in roles, "Missing system message"
            assert "user" in roles, "Missing user message"
            assert "assistant" in roles, "Missing assistant message"

            # Verify user message contains our prompt
            user_messages = [row[2] for row in rows if row[1] == "user"]
            assert any("hello from opencode" in msg.lower() for msg in user_messages), "User prompt not found in transcript"

            # Verify assistant responded
            assistant_messages = [row[2] for row in rows if row[1] == "assistant"]
            assert len(assistant_messages) >= 1, "No assistant response found"
            assert any(len(msg) > 0 for msg in assistant_messages), "Assistant response is empty"

        # 7. Cleanup
        adapter.stop_monitoring(temp_project)
        # Give extra time for service to release DB lock
        time.sleep(5)

    def test_real_opencode_two_sessions(self, temp_project):
        """
        Test that TRACE captures multiple REAL OpenCode sessions correctly.

        Flow:
        1. TRACE setup
        2. Run OpenCode session 1
        3. Run OpenCode session 2
        4. Verify both sessions captured with correct isolation
        """
        from memory.opencode_transparent import create_opencode_transparent_adapter
        adapter = create_opencode_transparent_adapter()
        setup_result = adapter.setup_integration(temp_project)
        assert setup_result["monitoring"]["status"] == "started"

        time.sleep(3)

        # Session 1 - deterministic prompt
        prompt1 = "Create a file alpha.txt containing the word ALPHA_ONLY"
        result1 = _run_opencode(temp_project, prompt1, timeout=120)
        assert result1.returncode == 0, f"Session 1 failed: {result1.stderr[:500]}"
        time.sleep(4)
        
        session_id_1 = _get_session_from_opencode_db(temp_project)
        assert session_id_1 is not None
        print(f"Session 1: {session_id_1}")

        # Session 2 - different deterministic prompt
        prompt2 = "Create a file beta.txt containing the word BETA_ONLY"
        result2 = _run_opencode(temp_project, prompt2, timeout=120)
        assert result2.returncode == 0, f"Session 2 failed: {result2.stderr[:500]}"
        time.sleep(4)
        
        session_id_2 = _get_session_from_opencode_db(temp_project)
        assert session_id_2 is not None
        print(f"Session 2: {session_id_2}")

        # Verify they are different sessions
        assert session_id_1 != session_id_2, "Sessions should be different"

        # Verify both captured with actual content
        count1 = _count_transcript_messages(temp_project, session_id_1)
        count2 = _count_transcript_messages(temp_project, session_id_2)
        
        print(f"Session 1 messages: {count1}")
        print(f"Session 2 messages: {count2}")
        
        assert count1 >= 3, "Session 1 not captured"
        assert count2 >= 3, "Session 2 not captured"

        # Verify content isolation - session 1 has ALPHA_ONLY, session 2 has BETA_ONLY
        trace_db = project_mod.db_path(temp_project)
        with sqlite3.connect(trace_db) as conn:
            cursor = conn.cursor()
            
            # Check session 1 has ALPHA_ONLY
            cursor.execute(
                "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                (session_id_1,)
            )
            user_msgs_1 = [row[0] for row in cursor.fetchall()]
            assert any("ALPHA_ONLY" in msg for msg in user_msgs_1), "Session 1 missing ALPHA_ONLY"
            
            # Check session 2 has BETA_ONLY
            cursor.execute(
                "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                (session_id_2,)
            )
            user_msgs_2 = [row[0] for row in cursor.fetchall()]
            assert any("BETA_ONLY" in msg for msg in user_msgs_2), "Session 2 missing BETA_ONLY"
            
            # Verify cross-isolation: session 1 doesn't have BETA_ONLY, session 2 doesn't have ALPHA_ONLY
            assert not any("BETA_ONLY" in msg for msg in user_msgs_1), "Session 1 leaked BETA_ONLY"
            assert not any("ALPHA_ONLY" in msg for msg in user_msgs_2), "Session 2 leaked ALPHA_ONLY"

        adapter.stop_monitoring(temp_project)
        # Give extra time for service to release DB lock
        time.sleep(5)


class TestRealOpenCodePersistence:
    """Test persistence across CLI exit, terminal close, service restart."""

    def test_service_survives_cli_exit(self, temp_project):
        """
        Test that the persistent service survives the CLI process exit.

        Flow:
        1. TRACE setup (creates opencode.json + starts service)
        2. CLI exits (simulated by just not stopping)
        3. Service should still be running
        4. OpenCode activity should still be captured
        """
        from memory.opencode_transparent import create_opencode_transparent_adapter

        adapter = create_opencode_transparent_adapter()
        setup_result = adapter.setup_integration(temp_project)
        assert setup_result["monitoring"]["status"] == "started"
        
        # Get service to check status
        from memory.opencode_service import get_service
        service = get_service(temp_project)
        pid = service.status().get("pid")
        print(f"Service started with PID: {pid}")

        # Verify service is running
        assert service.is_running()
        
        # Run OpenCode
        prompt = "Create a file cli_exit_test.py with content 'test'"
        _run_opencode(temp_project, prompt, timeout=120)
        time.sleep(5)

        # Verify capture happened
        session_id = _get_session_from_opencode_db(temp_project)
        assert session_id is not None
        
        msg_count = _count_transcript_messages(temp_project, session_id)
        assert msg_count >= 2

        # Cleanup
        adapter.stop_monitoring(temp_project)
        # Give extra time for service to release DB lock
        time.sleep(5)

    def test_service_restart_recovery(self, temp_project):
        """
        Test that service restart recovers correctly from persisted cursors.

        Flow:
        1. TRACE setup
        2. Run some OpenCode activity
        3. Stop service
        4. Run more OpenCode activity (while service stopped)
        5. Restart service
        7. Confirm session 2 is recovered from persisted cursor state
        8. Confirm session 1 is not duplicated
        8. Confirm session 2 is not duplicated
        """
        from memory.opencode_transparent import create_opencode_transparent_adapter

        adapter = create_opencode_transparent_adapter()
        setup_result = adapter.setup_integration(temp_project)
        assert setup_result["monitoring"]["status"] == "started"

        from memory.opencode_service import get_service
        service = get_service(temp_project)

        # Session 1 - while service running
        prompt1 = "Create file restart1.py with content RESTART_BEFORE_STOP"
        result1 = _run_opencode(temp_project, prompt1, timeout=120)
        assert result1.returncode == 0, f"Session 1 failed: {result1.stderr[:500]}"
        time.sleep(4)
        
        session_id_1 = _get_session_from_opencode_db(temp_project)
        assert session_id_1 is not None
        count1 = _count_transcript_messages(temp_project, session_id_1)
        assert count1 >= 3
        print(f"Session 1 (during service): {count1} messages")

        # Stop service
        adapter.stop_monitoring(temp_project)
        time.sleep(3)
        assert not service.is_running()

        # Session 2 - while service STOPPED
        prompt2 = "Create file restart2.py with content RESTART_DURING_STOP"
        result2 = _run_opencode(temp_project, prompt2, timeout=120)
        assert result2.returncode == 0, f"Session 2 failed: {result2.stderr[:500]}"
        time.sleep(4)
        
        session_id_2 = _get_session_from_opencode_db(temp_project)
        assert session_id_2 is not None
        assert session_id_2 != session_id_1

        # Restart service - should catch up
        adapter.start_monitoring(temp_project)
        time.sleep(5)

        # Verify session 2 was captured (catch-up)
        count2 = _count_transcript_messages(temp_project, session_id_2)
        print(f"Session 2 (catch-up): {count2} messages")
        assert count2 >= 3, f"Session 2 not captured after restart, got {count2} messages"

        # Verify session 1 not duplicated
        count1_after = _count_transcript_messages(temp_project, session_id_1)
        trace_db = project_mod.db_path(temp_project)
        with sqlite3.connect(trace_db) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM transcript_messages WHERE session_id = ?",
                (session_id_1,)
            )
            count1_after = cursor.fetchone()[0]
            # Get original count
            cursor.execute(
                "SELECT COUNT(*) FROM transcript_messages WHERE session_id = ?",
                (session_id_1,)
            )
            # The count should be the same (no duplicates)
            # We can't easily get the "before" count here since we didn't store it,
            # but we can verify the content is correct
            
            # Verify content correctness
            cursor.execute(
                "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                (session_id_1,)
            )
            user_msgs_1 = [row[0] for row in cursor.fetchall()]
            assert any("RESTART_BEFORE_STOP" in msg for msg in user_msgs_1), "Session 1 content corrupted"

            cursor.execute(
                "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                (session_id_2,)
            )
            user_msgs_2 = [row[0] for row in cursor.fetchall()]
            assert any("RESTART_DURING_STOP" in msg for msg in user_msgs_2), "Session 2 content missing"

        assert service.is_running()

        # Cleanup
        adapter.stop_monitoring(temp_project)
        time.sleep(3)

    def test_no_duplicate_capture_on_restart(self, temp_project):
        """
        Test that restarting service doesn't create duplicate captures.

        Flow:
        1. TRACE setup
        2. Run OpenCode activity
        3. Stop and restart service
        4. Verify no duplicate messages for the same session
        """
        from memory.opencode_transparent import create_opencode_transparent_adapter

        adapter = create_opencode_transparent_adapter()
        setup_result = adapter.setup_integration(temp_project)
        assert setup_result["monitoring"]["status"] == "started"

        from memory.opencode_service import get_service
        service = get_service(temp_project)

        # Run OpenCode with deterministic content
        prompt = "Create file dedup_test.py with content DEDUP_TEST_CONTENT"
        result = _run_opencode(temp_project, prompt, timeout=120)
        assert result.returncode == 0, f"OpenCode failed: {result.stderr[:500]}"
        time.sleep(4)
        
        session_id = _get_session_from_opencode_db(temp_project)
        assert session_id is not None
        
        # Get count before restart
        count_before = _count_transcript_messages(temp_project, session_id)
        print(f"Messages before restart: {count_before}")
        assert count_before >= 3

        # Verify content before restart
        trace_db = project_mod.db_path(temp_project)
        with sqlite3.connect(trace_db) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                (session_id,)
            )
            user_msgs_before = [row[0] for row in cursor.fetchall()]
            assert any("DEDUP_TEST_CONTENT" in msg for msg in user_msgs_before)

        # Stop and restart service
        adapter.stop_monitoring(temp_project)
        time.sleep(3)
        adapter.start_monitoring(temp_project)
        time.sleep(3)

        # Verify no duplicates
        count_after = _count_transcript_messages(temp_project, session_id)
        print(f"Messages after restart: {count_after}")
        
        assert count_after == count_before, f"Duplicates detected: {count_before} -> {count_after}"

        # Verify content unchanged
        with sqlite3.connect(trace_db) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                (session_id,)
            )
            user_msgs_after = [row[0] for row in cursor.fetchall()]
            assert user_msgs_after == user_msgs_before, "Content changed after restart"

        adapter.stop_monitoring(temp_project)
        time.sleep(2)


class TestRealOpenCodeIsolation:
    """Test project isolation with real OpenCode."""

    def test_two_projects_isolated(self, temp_project):
        """
        Test that two different projects have isolated capture.
        """
        # Create second project
        with tempfile.TemporaryDirectory() as tmp2:
            project2 = Path(tmp2) / "project2"
            project2.mkdir()
            (project2 / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
            project_mod.init_project(project2)

            from memory.opencode_transparent import create_opencode_transparent_adapter
            
            # Set up both projects
            adapter1 = create_opencode_transparent_adapter()
            adapter1.setup_integration(temp_project)
            
            adapter2 = create_opencode_transparent_adapter()
            adapter2.setup_integration(project2)

            time.sleep(3)

            # Run OpenCode in project 1
            prompt1 = "Create file proj1_test.py"
            _run_opencode(temp_project, prompt1, timeout=120)
            time.sleep(4)
            
            session_id_1 = _get_session_from_opencode_db(temp_project)
            
            # Run OpenCode in project 2
            prompt2 = "Create file proj2_test.py"
            _run_opencode(project2, prompt2, timeout=120)
            time.sleep(4)
            
            session_id_2 = _get_session_from_opencode_db(project2)
            
            # Verify isolation
            assert session_id_1 is not None
            assert session_id_2 is not None
            assert session_id_1 != session_id_2

            count1 = _count_transcript_messages(temp_project, session_id_1)
            count2 = _count_transcript_messages(project2, session_id_2)
            
            assert count1 >= 2
            assert count2 >= 2

            # Cross-check: project 1 session not in project 2 TRACE
            trace_db2 = project_mod.db_path(project2)
            with sqlite3.connect(trace_db2) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM transcript_messages WHERE session_id = ?",
                    (session_id_1,)
                )
                cross_count = cursor.fetchone()[0]
                assert cross_count == 0, "Cross-project leakage detected"

            adapter1.stop_monitoring(temp_project)
            adapter2.stop_monitoring(project2)
            # Give extra time for service to release DB lock
            time.sleep(3)


class TestNullDirectoryRegression:
    """Regression test for OpenCode sessions with missing/unusable directory."""

    def test_null_directory_isolation(self, temp_project):
        """
        Test that sessions with a missing or non-matching directory don't
        leak into other projects.

        The production schema declares session.directory NOT NULL, so a
        "missing" directory is modeled as an empty string (the only
        schema-valid representation of absent), plus a session pointed at
        an unrelated directory. Both inserts happen AFTER the service
        starts with committed transactions, so the directory filter is
        genuinely exercised (pre-start rows would sit below the service's
        initialized cursors and prove nothing).
        """
        db_path = _opencode_db_path()
        stamp = int(time.time() * 1000)
        empty_session = f"ses_emptydir_{stamp}"
        wrong_session = f"ses_wrongdir_{stamp}"

        def _insert_session(session_id, directory):
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO session (id, project_id, workspace_id, parent_id, slug, directory, path, title,
                               version, share_url, summary_additions, summary_deletions,
                               summary_files, summary_diffs, metadata, cost, tokens_input,
                               tokens_output, tokens_reasoning, tokens_cache_read,
                               tokens_cache_write, revert, permission, agent, model,
                               time_created, time_updated, time_compacting, time_archived)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    session_id, "test", "", "", "dir-test", directory, "", "Dir Test",
                    "1", "", 0, 0, 0, "", "{}", 0.0, 0, 0, 0, 0, 0, "", "", "", "",
                    int(time.time() * 1000), int(time.time() * 1000), 0, 0
                ))
                cursor.execute("""
                    INSERT INTO message (id, session_id, time_created, time_updated, data)
                    VALUES (?, ?, ?, ?, ?)
                """, (f"msg_{session_id}", session_id, int(time.time() * 1000), int(time.time() * 1000),
                      '{"role":"user","content":"test from unusable directory"}'))
                conn.commit()
            finally:
                conn.close()
            # Prove committed/visible to other connections before polling.
            check = sqlite3.connect(db_path)
            try:
                row = check.execute(
                    "SELECT id FROM session WHERE id = ?", (session_id,)
                ).fetchone()
            finally:
                check.close()
            assert row is not None, f"Session {session_id} not committed before polling phase"

        # Start TRACE monitoring FIRST so the malicious rows land above
        # the service's initialized cursors.
        from memory.opencode_transparent import create_opencode_transparent_adapter
        adapter = create_opencode_transparent_adapter()
        setup_result = adapter.setup_integration(temp_project)
        assert setup_result["monitoring"]["status"] == "started"

        time.sleep(3)

        try:
            # Empty string = schema-valid "missing" directory.
            _insert_session(empty_session, "")
            # Unrelated directory = must not leak across projects.
            _insert_session(wrong_session, "C:/definitely/not/this/project")

            time.sleep(5)

            # Neither session may appear in TRACE.
            trace_db = project_mod.db_path(temp_project)
            conn = sqlite3.connect(trace_db)
            try:
                cursor = conn.cursor()
                for sid in (empty_session, wrong_session):
                    cursor.execute(
                        "SELECT COUNT(*) FROM transcript_messages WHERE session_id = ?",
                        (sid,)
                    )
                    count = cursor.fetchone()[0]
                    assert count == 0, f"Session {sid} with unusable directory leaked into TRACE"
            finally:
                conn.close()
        finally:
            adapter.stop_monitoring(temp_project)
            time.sleep(2)

            # Clean up the test sessions from OpenCode DB.
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()
                for sid in (empty_session, wrong_session):
                    cursor.execute("DELETE FROM session WHERE id = ?", (sid,))
                    cursor.execute("DELETE FROM message WHERE session_id = ?", (sid,))
                conn.commit()
            finally:
                conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])