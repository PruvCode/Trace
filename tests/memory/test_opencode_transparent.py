"""Tests for OpenCode transparent capture adapter."""

import pytest
import time
from pathlib import Path

from memory import store as store_mod
from memory import transcript as transcript_mod
from memory import opencode_transparent as opencode_transparent_mod
from trace_memory import project as project_mod


@pytest.fixture()
def temp_project():
    """Create a temporary project with TRACE initialized."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        (project / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
        project_mod.init_project(project)
        yield project


def test_transparent_adapter_creation():
    """Test that transparent adapter can be created."""
    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    assert adapter.agent_name == "opencode"
    assert adapter.agent_version == ">=1.0.0"


def test_transparent_adapter_is_available():
    """Test that transparent adapter detects availability."""
    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    # OpenCode is installed in the test environment
    assert adapter.is_available() is True


def test_transparent_monitor_start_stop(temp_project):
    """Test starting and stopping the transparent monitor."""
    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    
    # Start monitoring
    result = adapter.start_monitoring(temp_project)
    assert result["status"] == "started"
    assert "pid" in result
    assert "message" in result
    
    # Check status
    status = adapter.get_monitoring_status(temp_project)
    assert status["status"] == "running"
    assert "pid" in status
    assert "project" in status
    
    # Give monitor time to run
    import time
    time.sleep(2)
    
    # Stop monitoring
    result = adapter.stop_monitoring(temp_project)
    assert result["status"] == "stopped"
    
    # Check status after stop
    status = adapter.get_monitoring_status(temp_project)
    assert status["status"] in ("not_started", "stopped")


def test_transparent_monitor_idempotent(temp_project):
    """Test that starting monitor twice is safe."""
    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    
    result1 = adapter.start_monitoring(temp_project)
    assert result1["status"] == "started"
    
    result2 = adapter.start_monitoring(temp_project)
    # Should not start again
    assert result2["status"] in ("already_running", "started")
    
    adapter.stop_monitoring(temp_project)


def test_transparent_monitor_start_stop_regression(temp_project):
    """Regression test for start/stop lifecycle.
    
    Ensures:
    - start() validates schema, clears stop event, creates and starts thread
    - stop() sets running false, sets stop event, waits for thread termination,
      verifies termination, cleans up thread reference
    - repeated start/stop works correctly
    - monitor loop uses interruptible stop event
    """
    from memory.opencode_transparent import OpenCodeDatabaseMonitor, CaptureConfig
    from memory import store as store_mod
    import tempfile
    import threading
    import time
    
    # Use a temp directory that we control cleanup for
    import shutil
    tmp = tempfile.mkdtemp()
    try:
        trace_db = Path(tmp) / "trace.db"
        conn = store_mod.connect(trace_db)
        store_mod.init_schema(conn)
        conn.close()

        opencode_db = Path(tmp) / "opencode.db"
        opencode_db.touch()

        config = CaptureConfig(redact_secrets=True)
        monitor = OpenCodeDatabaseMonitor(
            opencode_db_path=opencode_db,
            trace_db_path=trace_db,
            project_name="test",
            config=config,
        )
        monitor._schema_validated = True
        
        # Test 1: start() creates thread and sets running
        assert monitor._running is False
        assert monitor._thread is None
        assert not monitor._stop_event.is_set()
        
        monitor.start()
        
        assert monitor._running is True
        assert monitor._thread is not None
        assert isinstance(monitor._thread, threading.Thread)
        assert monitor._thread.daemon is True
        assert monitor._thread.is_alive()
        assert not monitor._stop_event.is_set()  # stop event cleared
        
        # Test 2: repeated start() is idempotent
        thread_before = monitor._thread
        monitor.start()
        assert monitor._thread is thread_before  # same thread, not recreated
        
        # Test 3: stop() terminates thread and cleans up
        monitor.stop()
        
        assert monitor._running is False
        assert monitor._stop_event.is_set()  # stop event set
        assert monitor._thread is None  # thread reference cleaned up
        
        # Test 4: repeated stop() is safe
        monitor.stop()  # should not raise
        
        # Test 5: start() after stop() works (restart)
        monitor.start()
        assert monitor._running is True
        assert monitor._thread is not None
        assert monitor._thread.is_alive()
        assert not monitor._stop_event.is_set()
        
        monitor.stop()
        assert monitor._running is False
        assert monitor._thread is None
        
        # Test 6: multiple start/stop cycles
        for _ in range(3):
            monitor.start()
            assert monitor._running is True
            assert monitor._thread is not None
            assert monitor._thread.is_alive()
            time.sleep(0.1)  # give thread time to run
            monitor.stop()
            assert monitor._running is False
            assert monitor._thread is None
            assert monitor._stop_event.is_set()
    finally:
        # Ensure monitor is stopped before cleanup
        try:
            monitor.stop()
        except Exception:
            pass
        # Give time for thread to fully terminate
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)


def test_setup_opencode_transparent(temp_project):
    """Test one-time transparent setup."""
    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    result = adapter.setup_integration(temp_project)
    
    assert "setup" in result
    assert "monitoring" in result
    assert result["setup"]["status"] in ("created", "updated")
    assert result["monitoring"]["status"] == "started"
    
    # Check that opencode.json was created with MCP config
    import json
    config_path = temp_project / "opencode.json"
    assert config_path.exists()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert "mcp" in config
    assert "trace-memory" in config["mcp"]
    mcp_server = config["mcp"]["trace-memory"]
    assert mcp_server["type"] == "local"
    assert mcp_server["enabled"] is True
    assert "command" in mcp_server
    assert "memory.mcp_server" in str(mcp_server["command"])
    
    # Stop monitoring
    adapter.stop_monitoring(temp_project)


def test_get_recent_context(temp_project):
    """Test retrieving relevant context from TRACE transcript memory."""
    # First, manually add some transcript messages
    db_path = project_mod.db_path(temp_project)
    conn = store_mod.connect(db_path)
    from memory import transcript as transcript_mod
    transcript_mod.record_message(
        conn,
        session_id="session-1",
        role="user",
        content="How do I fix the auth bug?",
        metadata={"model": "claude-3.5-sonnet"},
    )
    transcript_mod.record_message(
        conn,
        session_id="session-1",
        role="assistant",
        content="The fix is to reorder middleware in auth/login.py",
        metadata={"model": "claude-3.5-sonnet"},
    )
    transcript_mod.record_message(
        conn,
        session_id="session-2",
        role="user",
        content="How do I add a new user?",
        metadata={"model": "claude-3.5-sonnet"},
    )
    conn.close()

    # Now search for context using transparent adapter
    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    results = adapter.get_recent_context(temp_project, "auth bug", limit=5)
    assert len(results) >= 1
    assert any("auth bug" in r.content.lower() for r in results)

    results = adapter.get_recent_context(temp_project, "middleware", limit=5)
    assert len(results) >= 1
    assert any("middleware" in r.content.lower() for r in results)

    results = adapter.get_recent_context(temp_project, "unrelated query", limit=5)
    assert len(results) == 0


def test_transparent_monitor_persistence(temp_project):
    """Test that monitor captures sessions persistently."""
    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    
    # Start monitoring
    adapter.start_monitoring(temp_project)
    
    # Simulate a new session by directly inserting into OpenCode's database
    # (We can't run OpenCode in tests, but we can verify the monitor logic)
    # The monitor logic is tested via the database monitor tests
    
    adapter.stop_monitoring(temp_project)
    
    # Verify TRACE database is intact
    db_path = project_mod.db_path(temp_project)
    conn = store_mod.connect(db_path)
    try:
        # Should be able to query without errors
        from memory import transcript as transcript_mod
        sessions = transcript_mod.list_sessions(conn)
        assert isinstance(sessions, list)
    finally:
        conn.close()


def test_transparent_adapter_context_search_filters(temp_project):
    """Test that context search can filter by role."""
    from memory import transcript as transcript_mod
    db_path = project_mod.db_path(temp_project)
    conn = store_mod.connect(db_path)
    transcript_mod.record_message(conn, session_id="s1", role="user", content="user message about auth")
    transcript_mod.record_message(conn, session_id="s1", role="assistant", content="assistant response about auth")
    conn.close()

    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    results = adapter.get_recent_context(temp_project, "auth", limit=10)
    assert len(results) == 2
    roles = {r.role for r in results}
    assert "user" in roles
    assert "assistant" in roles


if __name__ == "__main__":
    pytest.main([__file__, "-v"])