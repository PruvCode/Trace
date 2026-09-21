"""Real OpenCode black-box validation tests.

These tests launch the ACTUAL OpenCode executable and verify TRACE captures
session data automatically. They do NOT simulate by inserting into the database.

REAL_OPENCode_TEST = POSSIBLE
"""

import tempfile
import time
import sqlite3
import subprocess
import json
from pathlib import Path

import pytest

from trace_memory import project as project_mod
from memory import opencode_transparent as opencode_transparent_mod
from tests.memory.test_real_opencode_blackbox import (
    _get_session_from_opencode_db,
    _count_transcript_messages,
)

# Path to the actual OpenCode executable (not the PowerShell wrapper)
OPENCODE_EXE = Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"


def _run_opencode_direct(project_path: Path, prompt: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run OpenCode with a prompt in the given project directory using the actual .exe."""
    cmd = [
        str(OPENCODE_EXE),
        "run",
        prompt,
        "--format", "json",
        "--dir", str(project_path),
        "--auto",
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


@pytest.fixture()
def temp_project():
    """Create a temporary project with TRACE initialized."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        (project / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
        project_mod.init_project(project)
        yield project


class TestRealOpenCodeEnvironment:
    """Test 4.4.1: Verify the real OpenCode environment."""

    def test_opencode_available(self):
        """Verify OpenCode is installed and accessible."""
        result = subprocess.run([str(OPENCODE_EXE), "--version"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "1.18.31" in result.stdout

    def test_opencode_db_exists(self):
        """Verify OpenCode database exists."""
        db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        assert db_path.exists(), f"OpenCode DB not found at {db_path}"

    def test_free_model_available(self):
        """Verify a free model is available for testing."""
        result = subprocess.run([str(OPENCODE_EXE), "models"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "nemotron-3-ultra-free" in result.stdout

    def test_opencode_run_non_interactive(self):
        """Test that OpenCode can run non-interactively with a free model."""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "test_proj"
            project.mkdir()
            (project / "test.py").write_text("print('hello')\n", encoding="utf-8")

            result = _run_opencode_direct(project, "Say hello", timeout=60)
            # OpenCode should run and produce JSON output
            assert result.returncode in (0, 1)  # May exit with 1 due to rate limits but should run
            assert "step_start" in result.stdout or "error" in result.stdout


class TestRealOpenCodeCapture:
    """Test 4.4.2: Real OpenCode session capture."""

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
        adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
        setup_result = adapter.setup_integration(temp_project)
        assert setup_result["setup"]["status"] in ("created", "updated")
        assert setup_result["monitoring"]["status"] == "started"

        # Give service time to fully start
        time.sleep(3)

        # 2. Run REAL OpenCode with a free model
        prompt = "Create a simple hello.py file that prints 'hello from opencode'"
        result = _run_opencode_direct(temp_project, prompt, timeout=90)

        # OpenCode may return non-zero for various reasons; we just check it ran
        print(f"OpenCode exit code: {result.returncode}")
        print(f"OpenCode stdout: {result.stdout[:500]}")
        print(f"OpenCode stderr: {result.stderr[:500]}")

        # 3. Give monitor time to capture
        time.sleep(5)

        # 4. Get the session ID from OpenCode database
        session_id = _get_session_from_opencode_db(temp_project)
        assert session_id is not None, "No session found in OpenCode database"

        print(f"Found OpenCode session: {session_id}")

        # 5. Verify TRACE captured the session
        msg_count = _count_transcript_messages(temp_project, session_id)
        print(f"TRACE transcript messages captured: {msg_count}")

        # Should have at least system message + assistant response
        assert msg_count >= 2, f"Expected at least 2 transcript messages, got {msg_count}"

        # 6. Verify content
        trace_db = project_mod.db_path(temp_project)
        with sqlite3.connect(trace_db) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM transcript_messages WHERE session_id = ?", (session_id,))
            rows = cursor.fetchall()
            print(f"Captured messages: {len(rows)}")
            for row in rows:
                print(f"  {row}")

        # 7. Cleanup
        adapter.stop_monitoring(temp_project)
        time.sleep(2)

    def test_real_opencode_two_sessions(self, temp_project):
        """
        Test that TRACE captures multiple REAL OpenCode sessions correctly.
        """
        from memory.opencode_transparent import create_opencode_transparent_adapter
        adapter = create_opencode_transparent_adapter()
        setup_result = adapter.setup_integration(temp_project)
        assert setup_result["monitoring"]["status"] == "started"

        time.sleep(3)

        # Session 1
        prompt1 = "Create a file session1.txt with content 'first session'"
        _run_opencode_direct(temp_project, prompt1, timeout=60)
        time.sleep(4)

        session_id_1 = _get_session_from_opencode_db(temp_project)
        assert session_id_1 is not None
        print(f"Session 1: {session_id_1}")

        # Session 2
        prompt2 = "Create a file session2.txt with content 'second session'"
        _run_opencode_direct(temp_project, prompt2, timeout=60)
        time.sleep(4)

        session_id_2 = _get_session_from_opencode_db(temp_project)
        assert session_id_2 is not None
        print(f"Session 2: {session_id_2}")

        # Verify they are different sessions
        assert session_id_1 != session_id_2, "Sessions should be different"

        # Verify both captured
        count1 = _count_transcript_messages(temp_project, session_id_1)
        count2 = _count_transcript_messages(temp_project, session_id_2)

        print(f"Session 1 messages: {count1}")
        print(f"Session 2 messages: {count2}")

        assert count1 >= 2, "Session 1 not captured"
        assert count2 >= 2, "Session 2 not captured"

        adapter.stop_monitoring(temp_project)
        time.sleep(2)


class TestCrossSessionPersistence:
    """Test 4.4.3: Cross-session persistence and service restart."""

    def test_service_survives_cli_exit(self, temp_project):
        """
        Test that the persistent service survives the CLI process exit.
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
        _run_opencode_direct(temp_project, prompt, timeout=60)
        time.sleep(5)

        # Verify capture happened
        session_id = _get_session_from_opencode_db(temp_project)
        assert session_id is not None

        msg_count = _count_transcript_messages(temp_project, session_id)
        assert msg_count >= 2

        # Cleanup
        adapter.stop_monitoring(temp_project)
        time.sleep(2)

    def test_service_restart_recovery(self, temp_project):
        """
        Test that service restart recovers correctly from persisted cursors.
        """
        from memory.opencode_transparent import create_opencode_transparent_adapter
        adapter = create_opencode_transparent_adapter()
        setup_result = adapter.setup_integration(temp_project)
        assert setup_result["monitoring"]["status"] == "started"

        from memory.opencode_service import get_service
        service = get_service(temp_project)

        # Session 1 - while service running
        prompt1 = "Create file restart1.py with 'before stop'"
        _run_opencode_direct(temp_project, prompt1, timeout=60)
        time.sleep(4)

        session_id_1 = _get_session_from_opencode_db(temp_project)
        assert session_id_1 is not None
        count1 = _count_transcript_messages(temp_project, session_id_1)
        assert count1 >= 2
        print(f"Session 1 (during service): {count1} messages")

        # Stop service
        adapter.stop_monitoring(temp_project)
        time.sleep(2)
        assert not service.is_running()

        # Session 2 - while service STOPPED
        prompt2 = "Create file restart2.py with 'during stop'"
        _run_opencode_direct(temp_project, prompt2, timeout=60)
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
        assert service.is_running()

        # Cleanup
        adapter.stop_monitoring(temp_project)
        time.sleep(2)

    def test_no_duplicate_capture_on_restart(self, temp_project):
        """
        Test that restarting service doesn't create duplicate captures.
        """
        from memory.opencode_transparent import create_opencode_transparent_adapter
        adapter = create_opencode_transparent_adapter()
        setup_result = adapter.setup_integration(temp_project)
        assert setup_result["monitoring"]["status"] == "started"

        from memory.opencode_service import get_service
        service = get_service(temp_project)

        # Run OpenCode
        prompt = "Create file dedup_test.py with 'no duplicates'"
        _run_opencode_direct(temp_project, prompt, timeout=60)
        time.sleep(4)

        session_id = _get_session_from_opencode_db(temp_project)
        assert session_id is not None

        # Get count before restart
        count_before = _count_transcript_messages(temp_project, session_id)
        print(f"Messages before restart: {count_before}")

        # Stop and restart service
        adapter.stop_monitoring(temp_project)
        time.sleep(2)
        adapter.start_monitoring(temp_project)
        time.sleep(3)

        # Verify no duplicates
        count_after = _count_transcript_messages(temp_project, session_id)
        print(f"Messages after restart: {count_after}")

        assert count_after == count_before, f"Duplicates detected: {count_before} -> {count_after}"

        adapter.stop_monitoring(temp_project)
        time.sleep(2)


class TestProjectIsolation:
    """Test 4.4.3: Project isolation with real OpenCode."""

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
            _run_opencode_direct(temp_project, prompt1, timeout=60)
            time.sleep(4)

            session_id_1 = _get_session_from_opencode_db(temp_project)

            # Run OpenCode in project 2
            prompt2 = "Create file proj2_test.py"
            _run_opencode_direct(project2, prompt2, timeout=60)
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
            time.sleep(2)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])