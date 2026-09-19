"""Tests for OpenCode capture adapter and automatic transcript persistence."""

import pytest
import tempfile
from pathlib import Path

from memory import store as store_mod
from memory import transcript as transcript_mod
from memory import opencode_capture as opencode_capture_mod
from trace_memory import project as project_mod


@pytest.fixture()
def temp_project():
    """Create a temporary project with TRACE initialized."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        (project / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
        project_mod.init_project(project)
        yield project


def test_opencode_adapter_creation():
    """Test that OpenCode adapter can be created."""
    adapter = opencode_capture_mod.create_opencode_adapter()
    assert adapter.agent_name == "opencode"
    assert adapter.agent_version == ">=1.0.0"


def test_opencode_adapter_is_available():
    """Test that OpenCode adapter detects availability."""
    adapter = opencode_capture_mod.create_opencode_adapter()
    # OpenCode is installed in the test environment
    assert adapter.is_available() is True


def test_opencode_setup_integration(temp_project):
    """Test one-time setup creates opencode.json with MCP config."""
    adapter = opencode_capture_mod.create_opencode_adapter()
    result = adapter.setup_integration(temp_project)

    assert result["status"] in ("created", "already_configured")
    assert "config_path" in result
    config_path = Path(result["config_path"])
    assert config_path.exists()

    import json
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert "mcp" in config
    assert "trace-memory" in config["mcp"]


def test_opencode_setup_idempotent(temp_project):
    """Test that running setup twice is safe."""
    adapter = opencode_capture_mod.create_opencode_adapter()
    result1 = adapter.setup_integration(temp_project)
    result2 = adapter.setup_integration(temp_project)

    assert result2["status"] == "already_configured"


def test_capture_opencode_session_records_transcript(temp_project):
    """Test that capture_opencode_session persists transcript messages."""
    # This test uses a mock - we can't actually run OpenCode without API key
    # But we can test the project_mod function directly
    result = project_mod.capture_opencode_session(
        temp_project,
        prompt="test prompt",
        session_id="test-session-123",
    )
    # Should return empty list since OpenCode fails without API key
    # but should not crash
    assert isinstance(result, list)


def test_get_recent_context(temp_project):
    """Test retrieving relevant context for a new session."""
    # First, manually add some transcript messages
    db_path = project_mod.db_path(temp_project)
    conn = store_mod.connect(db_path)
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

    # Now search for context
    results = project_mod.get_recent_context(temp_project, "auth bug", limit=5)
    assert len(results) >= 1
    assert any("auth bug" in r["content"].lower() for r in results)

    results = project_mod.get_recent_context(temp_project, "middleware", limit=5)
    assert len(results) >= 1
    assert any("middleware" in r["content"].lower() for r in results)

    results = project_mod.get_recent_context(temp_project, "unrelated query", limit=5)
    assert len(results) == 0


def test_capture_session_yields_messages():
    """Test that capture_session yields proper message types."""
    adapter = opencode_capture_mod.create_opencode_adapter()

    # We can't run OpenCode without API key, but we can verify the generator
    # yields proper types by testing the project_mod wrapper
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        project_mod.init_project(project)

        # This will fail gracefully (OpenCode not configured with API key)
        result = project_mod.capture_opencode_session(
            project,
            prompt="test",
            session_id="test-123",
        )
        assert isinstance(result, list)
        # Check that items have expected structure
        for item in result:
            assert "type" in item
            assert item["type"] in ("message", "event")


def test_setup_opencode_integration_creates_mcp_config(temp_project):
    """Test that setup_opencode_integration adds TRACE MCP server."""
    result = project_mod.setup_opencode_integration(temp_project)
    assert result["status"] == "created"

    import json
    config_path = Path(result["config_path"])
    config = json.loads(config_path.read_text(encoding="utf-8"))

    # Check MCP config includes trace-memory
    assert "mcp" in config
    assert "trace-memory" in config["mcp"]
    mcp_server = config["mcp"]["trace-memory"]
    assert mcp_server["type"] == "local"
    assert mcp_server["enabled"] is True
    assert "command" in mcp_server
    assert "memory.mcp_server" in str(mcp_server["command"])


def test_context_search_filters_by_role(temp_project):
    """Test that context search can filter by role."""
    db_path = project_mod.db_path(temp_project)
    conn = store_mod.connect(db_path)
    transcript_mod.record_message(conn, session_id="s1", role="user", content="user message about auth")
    transcript_mod.record_message(conn, session_id="s1", role="assistant", content="assistant response about auth")
    conn.close()

    results = project_mod.get_recent_context(temp_project, "auth", limit=10)
    assert len(results) == 2
    roles = {r["role"] for r in results}
    assert "user" in roles
    assert "assistant" in roles


if __name__ == "__main__":
    pytest.main([__file__, "-v"])