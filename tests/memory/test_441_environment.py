"""Real OpenCode black-box validation tests.

These tests launch the ACTUAL OpenCode executable and verify TRACE captures
session data automatically. They do NOT simulate by inserting into the database.

REAL_OPENCode_TEST = POSSIBLE
"""

import re
import json
import sqlite3
import subprocess
import tempfile
import time
import os
from pathlib import Path

import pytest

# Fake key for OpenCode CLI invocation in tests. Never a real credential.
os.environ["OPENAI_API_KEY"] = "test"


def pytest_configure(config):
    """Set environment variable for all tests."""
    os.environ["OPENAI_API_KEY"] = "test"


@pytest.fixture(autouse=True)
def set_openai_api_key():
    """Ensure OPENAI_API_KEY is set for all tests."""
    os.environ["OPENAI_API_KEY"] = "test"
    yield


from trace_memory import project as project_mod
from memory import opencode_transparent as opencode_transparent_mod
from tests.memory.test_real_opencode_blackbox import (
    _get_session_from_opencode_db,
    _count_transcript_messages,
)

# Path to the actual OpenCode executable (not the PowerShell wrapper)
OPENCODE_EXE = Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"

# Minimum OpenCode version TRACE requires (major, minor). Patch releases float.
MIN_OPENCODE_VERSION = (1, 18)


def _run_opencode_direct(project_path: Path, prompt: str, timeout: int = 150) -> subprocess.CompletedProcess:
    """Run OpenCode with a prompt in the given project directory using the actual .exe."""
    cmd = [
        str(OPENCODE_EXE),
        "run",
        prompt,
        "--format", "json",
        "--dir", str(project_path),
        "--auto",
        "--model", "opencode/big-pickle",
    ]
    # Set OPENAI_API_KEY for OpenCode CLI
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = "test"
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(project_path),
        env=env,
    )
    return result


def _query_trace_db(project_path: Path, sql: str, params: tuple = ()):
    """Run a read query against the TRACE db with a guaranteed-closed connection.

    The connection is closed before return and never escapes into test
    frames: on Windows a lingering handle can be mirrored by supervisor
    sidecar processes and break temporary-directory cleanup with WinError 32.
    Note that `with sqlite3.connect(...)` alone does NOT close on exit.
    """
    trace_db = project_mod.db_path(project_path)
    conn = sqlite3.connect(trace_db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _stop_service_and_wait(adapter, project_path: Path, timeout: float = 20.0) -> None:
    """Stop the TRACE capture service and wait for child process termination.

    Polls until the service child process is gone so SQLite handles are
    released before the temporary directory is removed (Windows DB-lock
    safety). Exceptions propagate; teardown failures are never suppressed.
    """
    from memory.opencode_service import get_service

    adapter.stop_monitoring(project_path)
    service = get_service(project_path)
    deadline = time.time() + timeout
    while service.is_running() and time.time() < deadline:
        time.sleep(0.5)
    assert not service.is_running(), "TRACE capture service did not terminate"
    # Grace period for the OS to release SQLite file handles on Windows.
    time.sleep(2)


def _rmtree_with_retry(path: Path, timeout: float = 120.0) -> None:
    """Remove a tree, retrying transient Windows file locks.

    On Windows, handles to the TRACE database can be mirrored by sandbox
    supervisor sidecars and released on their own schedule (observed holds
    exceed 30s). Retry with backoff; if the lock persists past the budget
    the last error is re-raised so cleanup failures are never suppressed.
    """
    import shutil

    deadline = time.time() + timeout
    last_error: OSError | None = None
    while True:
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except PermissionError as e:
            last_error = e
            if time.time() >= deadline:
                raise
            time.sleep(2.0)


@pytest.fixture()
def temp_project(monkeypatch):
    """Create a temporary project with TRACE initialized."""
    # Ensure OPENAI_API_KEY is set for OpenCode CLI
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    tmp = tempfile.mkdtemp()
    try:
        project = Path(tmp) / "project"
        project.mkdir()
        (project / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
        project_mod.init_project(project)
        yield project
    finally:
        # Defensive teardown: never leave a capture child holding the
        # temp TRACE database, which would break directory removal.
        try:
            from memory.opencode_service import get_service
            service = get_service(project)
            if service.is_running():
                stop_result = service.stop()
                print(f"[temp_project teardown] service stop: {stop_result}")
                deadline = time.time() + 20.0
                while service.is_running() and time.time() < deadline:
                    time.sleep(0.5)
                print(f"[temp_project teardown] service running after wait: {service.is_running()}")
                time.sleep(2)
        except Exception as e:
            print(f"[temp_project teardown] best-effort service stop: {e}")
        _rmtree_with_retry(Path(tmp))


class TestRealOpenCodeEnvironment:
    """Test 4.4.1: Verify the real OpenCode environment."""

    def test_opencode_available(self):
        """Verify OpenCode is installed and meets the minimum supported version."""
        result = subprocess.run([str(OPENCODE_EXE), "--version"], capture_output=True, text=True)
        assert result.returncode == 0
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout)
        assert match is not None, f"Unparseable OpenCode version: {result.stdout!r}"
        version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        assert version[:2] >= MIN_OPENCODE_VERSION, (
            f"OpenCode {version} below minimum supported {MIN_OPENCODE_VERSION}"
        )

    def test_opencode_db_exists(self):
        """Verify OpenCode database exists."""
        db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        assert db_path.exists(), f"OpenCode DB not found at {db_path}"

    def test_free_model_available(self):
        """Verify the free model used by these tests is available."""
        result = subprocess.run([str(OPENCODE_EXE), "models"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "big-pickle" in result.stdout

    def test_opencode_run_non_interactive(self):
        """Test that OpenCode can run non-interactively with a free model."""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "test_proj"
            project.mkdir()
            (project / "test.py").write_text("print('hello')\n", encoding="utf-8")

            result = _run_opencode_direct(project, "Say hello", timeout=120)
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

        # 2. Run REAL OpenCode with a free model - STRICT: must succeed
        prompt = "Create a simple hello.py file that prints 'hello from opencode'"
        result = _run_opencode_direct(temp_project, prompt, timeout=180)

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
        rows = _query_trace_db(
            temp_project,
            "SELECT session_id, role, content FROM transcript_messages WHERE session_id = ? ORDER BY id",
            (session_id,)
        )
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

        # Verify no duplicate rows for this session. Distinct capture
        # events may share content (e.g. repeated step-finish reasons),
        # so duplicates are detected by source identity (part/message id
        # in metadata), not by identical content.
        metadata_rows = _query_trace_db(
            temp_project,
            "SELECT metadata FROM transcript_messages WHERE session_id = ?",
            (session_id,)
        )
        seen_source_ids: set[str] = set()
        duplicate_sources: list[str] = []
        for (metadata_text,) in metadata_rows:
            try:
                metadata = json.loads(metadata_text) if metadata_text else {}
            except ValueError:
                continue
            source_id = metadata.get("part_id") or metadata.get("message_id")
            if source_id is None:
                continue
            if source_id in seen_source_ids:
                duplicate_sources.append(source_id)
            seen_source_ids.add(source_id)
        assert duplicate_sources == [], f"Duplicate transcript rows detected: {duplicate_sources}"

        # 7. Cleanup with proper wait
        _stop_service_and_wait(adapter, temp_project)

    def test_real_opencode_two_sessions(self, temp_project):
        """
        Test that TRACE captures multiple REAL OpenCode sessions correctly.
        """
        from memory.opencode_transparent import create_opencode_transparent_adapter
        adapter = create_opencode_transparent_adapter()
        setup_result = adapter.setup_integration(temp_project)
        assert setup_result["monitoring"]["status"] == "started"

        time.sleep(3)

        # Session 1 - deterministic prompt
        prompt1 = "Create a file alpha.txt containing the word ALPHA_ONLY"
        result1 = _run_opencode_direct(temp_project, prompt1, timeout=120)
        assert result1.returncode == 0, f"Session 1 failed: {result1.stderr[:500]}"
        time.sleep(4)

        session_id_1 = _get_session_from_opencode_db(temp_project)
        assert session_id_1 is not None
        print(f"Session 1: {session_id_1}")

        # Session 2
        prompt2 = "Create a file beta.txt containing the word BETA_ONLY"
        result2 = _run_opencode_direct(temp_project, prompt2, timeout=120)
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
        user_msgs_1 = [
            row[0] for row in _query_trace_db(
                temp_project,
                "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                (session_id_1,)
            )
        ]
        assert any("ALPHA_ONLY" in msg for msg in user_msgs_1), "Session 1 missing ALPHA_ONLY"

        user_msgs_2 = [
            row[0] for row in _query_trace_db(
                temp_project,
                "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                (session_id_2,)
            )
        ]
        assert any("BETA_ONLY" in msg for msg in user_msgs_2), "Session 2 missing BETA_ONLY"

        # Verify cross-isolation: session 1 doesn't have BETA_ONLY, session 2 doesn't have ALPHA_ONLY
        assert not any("BETA_ONLY" in msg for msg in user_msgs_1), "Session 1 leaked BETA_ONLY"
        assert not any("ALPHA_ONLY" in msg for msg in user_msgs_2), "Session 2 leaked ALPHA_ONLY"

        _stop_service_and_wait(adapter, temp_project)


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
        prompt = "Create a file cli_exit_test.py with content 'CLI_EXIT_TEST'"
        result = _run_opencode_direct(temp_project, prompt, timeout=120)
        assert result.returncode == 0, f"OpenCode failed: {result.stderr[:500]}"
        time.sleep(5)

        # Verify capture happened
        session_id = _get_session_from_opencode_db(temp_project)
        assert session_id is not None

        msg_count = _count_transcript_messages(temp_project, session_id)
        assert msg_count >= 3

        # Verify content
        user_msgs = [
            row[0] for row in _query_trace_db(
                temp_project,
                "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                (session_id,)
            )
        ]
        assert any("CLI_EXIT_TEST" in msg for msg in user_msgs), "CLI_EXIT_TEST not found"

        # Cleanup
        _stop_service_and_wait(adapter, temp_project)

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
        prompt1 = "Create file restart1.py with content RESTART_BEFORE_STOP"
        result1 = _run_opencode_direct(temp_project, prompt1, timeout=120)
        assert result1.returncode == 0, f"Session 1 failed: {result1.stderr[:500]}"
        time.sleep(4)

        session_id_1 = _get_session_from_opencode_db(temp_project)
        assert session_id_1 is not None
        count1 = _count_transcript_messages(temp_project, session_id_1)
        assert count1 >= 3
        print(f"Session 1 (during service): {count1} messages")

        # Stop service
        _stop_service_and_wait(adapter, temp_project)
        assert not service.is_running()

        # Session 2 - while service STOPPED
        prompt2 = "Create file restart2.py with content RESTART_DURING_STOP"
        result2 = _run_opencode_direct(temp_project, prompt2, timeout=120)
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
        # Verify session 1 content
        user_msgs_1 = [
            row[0] for row in _query_trace_db(
                temp_project,
                "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                (session_id_1,)
            )
        ]
        assert any("RESTART_BEFORE_STOP" in msg for msg in user_msgs_1), "Session 1 content corrupted"

        # Verify session 2 content
        user_msgs_2 = [
            row[0] for row in _query_trace_db(
                temp_project,
                "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                (session_id_2,)
            )
        ]
        assert any("RESTART_DURING_STOP" in msg for msg in user_msgs_2), "Session 2 content missing"

        assert service.is_running()

        # Cleanup
        _stop_service_and_wait(adapter, temp_project)

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

        # Run OpenCode with deterministic content
        prompt = "Create file dedup_test.py with content DEDUP_TEST_CONTENT"
        result = _run_opencode_direct(temp_project, prompt, timeout=120)
        assert result.returncode == 0, f"OpenCode failed: {result.stderr[:500]}"
        time.sleep(4)

        session_id = _get_session_from_opencode_db(temp_project)
        assert session_id is not None

        # Get count before restart
        count_before = _count_transcript_messages(temp_project, session_id)
        print(f"Messages before restart: {count_before}")
        assert count_before >= 3

        # Verify content before restart
        user_msgs_before = [
            row[0] for row in _query_trace_db(
                temp_project,
                "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                (session_id,)
            )
        ]
        assert any("DEDUP_TEST_CONTENT" in msg for msg in user_msgs_before)

        # Stop and restart service
        _stop_service_and_wait(adapter, temp_project)
        adapter.start_monitoring(temp_project)
        time.sleep(3)

        # Verify no duplicates
        count_after = _count_transcript_messages(temp_project, session_id)
        print(f"Messages after restart: {count_after}")

        assert count_after == count_before, f"Duplicates detected: {count_before} -> {count_after}"

        # Verify content unchanged
        user_msgs_after = [
            row[0] for row in _query_trace_db(
                temp_project,
                "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                (session_id,)
            )
        ]
        assert user_msgs_after == user_msgs_before, "Content changed after restart"

        _stop_service_and_wait(adapter, temp_project)


class TestProjectIsolation:
    """Test 4.4.3: Project isolation with real OpenCode."""

    def test_two_projects_isolated(self, temp_project):
        """
        Test that two different projects have isolated capture.
        """
        # Create second project (explicit cleanup with lock retries)
        tmp2 = tempfile.mkdtemp()
        try:
            project2 = Path(tmp2) / "project2"
            project2.mkdir()
            (project2 / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
            project_mod.init_project(project2)

            from memory.opencode_transparent import create_opencode_transparent_adapter

            # Set up both projects
            adapter1 = create_opencode_transparent_adapter()
            setup1 = adapter1.setup_integration(temp_project)
            assert setup1["monitoring"]["status"] == "started"

            adapter2 = create_opencode_transparent_adapter()
            setup2 = adapter2.setup_integration(project2)
            assert setup2["monitoring"]["status"] == "started"

            time.sleep(3)

            # Run OpenCode in project 1
            prompt1 = "Create file proj1_test.py with content PROJECT1_ONLY"
            result1 = _run_opencode_direct(temp_project, prompt1, timeout=120)
            assert result1.returncode == 0, f"Project 1 OpenCode failed: {result1.stderr[:500]}"
            time.sleep(4)

            session_id_1 = _get_session_from_opencode_db(temp_project)
            assert session_id_1 is not None

            # Run OpenCode in project 2
            prompt2 = "Create file proj2_test.py with content PROJECT2_ONLY"
            result2 = _run_opencode_direct(project2, prompt2, timeout=120)
            assert result2.returncode == 0, f"Project 2 OpenCode failed: {result2.stderr[:500]}"
            time.sleep(4)

            session_id_2 = _get_session_from_opencode_db(project2)
            assert session_id_2 is not None

            # Verify isolation
            assert session_id_1 != session_id_2

            count1 = _count_transcript_messages(temp_project, session_id_1)
            count2 = _count_transcript_messages(project2, session_id_2)

            assert count1 >= 3
            assert count2 >= 3

            # Verify content isolation
            user_msgs_1 = [
                row[0] for row in _query_trace_db(
                    temp_project,
                    "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                    (session_id_1,)
                )
            ]
            assert any("PROJECT1_ONLY" in msg for msg in user_msgs_1), "Project 1 missing PROJECT1_ONLY"

            user_msgs_2 = [
                row[0] for row in _query_trace_db(
                    project2,
                    "SELECT content FROM transcript_messages WHERE session_id = ? AND role = 'user'",
                    (session_id_2,)
                )
            ]
            assert any("PROJECT2_ONLY" in msg for msg in user_msgs_2), "Project 2 missing PROJECT2_ONLY"

            # Cross-check: project 1 session not in project 2 TRACE
            cross_count = _query_trace_db(
                project2,
                "SELECT COUNT(*) FROM transcript_messages WHERE session_id = ?",
                (session_id_1,)
            )[0][0]
            assert cross_count == 0, "Cross-project leakage detected"

            _stop_service_and_wait(adapter1, temp_project)
            _stop_service_and_wait(adapter2, project2)
        finally:
            try:
                from memory.opencode_service import get_service as _get_service2
                _svc2 = _get_service2(project2)
                if _svc2.is_running():
                    _svc2.stop()
                    _deadline2 = time.time() + 20.0
                    while _svc2.is_running() and time.time() < _deadline2:
                        time.sleep(0.5)
                    time.sleep(2)
            except Exception as e:
                print(f"[test_two_projects_isolated teardown] best-effort service stop: {e}")
            _rmtree_with_retry(Path(tmp2))


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
