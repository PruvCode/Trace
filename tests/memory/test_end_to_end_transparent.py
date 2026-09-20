"""End-to-end test for transparent OpenCode capture across sessions.

This test verifies the complete user workflow:
1. One-time setup with `trace setup opencode --transparent`
2. Start OpenCode normally (simulated by inserting into OpenCode DB)
3. TRACE monitor captures session automatically
4. User exits OpenCode
5. New OpenCode session starts
6. Previous context is available through MCP retrieval

This test simulates the full flow by directly manipulating OpenCode's database
and verifying TRACE captures and persists the data correctly.
"""

import time
import sqlite3
import tempfile
from pathlib import Path

import pytest

from memory.opencode_transparent import create_opencode_transparent_adapter
from memory import store as store_mod
from memory import transcript as transcript_mod
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


def test_end_to_end_transparent_capture(temp_project):
    """
    Test the complete transparent capture workflow:
    1. Set up transparent monitoring
    2. Simulate OpenCode session (insert into OpenCode DB)
    3. Verify TRACE captures session
    4. Simulate new OpenCode session
    5. Verify retrieval works
    """
    # Step 1: One-time setup
    adapter = create_opencode_transparent_adapter()
    setup_result = adapter.setup_integration(temp_project)
    print(f"Setup result: {setup_result}")
    assert setup_result["setup"]["status"] in ("created", "updated")
    assert setup_result["monitoring"]["status"] == "started"
    
    # Verify opencode.json was created with MCP config
    import json
    config_path = temp_project / "opencode.json"
    assert config_path.exists()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert "mcp" in config
    assert "trace-memory" in config["mcp"]
    
    # Give service time to fully start
    time.sleep(3)
    
    # Check service status
    status = adapter.get_monitoring_status(temp_project)
    print(f"Service status after 3s: {status}", flush=True)
    
    # Step 2: Simulate Session 1 - User codes with OpenCode
    # We simulate by inserting directly into OpenCode's database
    opencode_db = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    session_id = f"ses_e2e_test_session_1_{int(time.time() * 1000)}"
    
    with sqlite3.connect(opencode_db) as conn:
        cursor = conn.cursor()
        # Insert session using dict-based insert to avoid column count issues
        session_data = {
            'id': session_id,
            'project_id': 'test_project',
            'workspace_id': '',
            'parent_id': '',
            'slug': 'e2e-test-session-1',
            'directory': str(temp_project),
            'path': '',
            'title': 'E2E Test Session 1',
            'version': '1',
            'share_url': '',
            'summary_additions': 0,
            'summary_deletions': 0,
            'summary_files': 0,
            'summary_diffs': '',
            'metadata': '{}',
            'cost': 0.0,
            'tokens_input': 0,
            'tokens_output': 0,
            'tokens_reasoning': 0,
            'tokens_cache_read': 0,
            'tokens_cache_write': 0,
            'revert': '',
            'permission': '',
            'agent': '',
            'model': '',
            'time_created': int(time.time() * 1000),
            'time_updated': int(time.time() * 1000),
            'time_compacting': 0,
            'time_archived': 0,
        }
        cols = ', '.join(session_data.keys())
        placeholders = ', '.join(['?' for _ in session_data])
        cursor.execute(f"INSERT INTO session ({cols}) VALUES ({placeholders})", list(session_data.values()))
        
        # Insert user message
        msg_id = f"msg_e2e_user_1_{int(time.time() * 1000)}"
        cursor.execute("""
            INSERT INTO message (id, session_id, time_created, time_updated, data)
            VALUES (?, ?, ?, ?, ?)
        """, (msg_id, session_id, int(time.time() * 1000), int(time.time() * 1000), 
              '{"role":"user","content":"Create a quantum_banana.py module"}'))
        
        # Insert assistant response
        msg_id_2 = f"msg_e2e_assistant_1_{int(time.time() * 1000)}"
        cursor.execute("""
            INSERT INTO message (id, session_id, time_created, time_updated, data)
            VALUES (?, ?, ?, ?, ?)
        """, (msg_id_2, session_id, int(time.time() * 1000) + 1000, int(time.time() * 1000) + 1000,
              '{"role":"assistant","content":"I will create a quantum_banana.py module with a calculate_banana_tax function"}'))
        
        # Insert tool call (memory tool)
        part_id = f"prt_e2e_tool_1_{int(time.time() * 1000)}"
        cursor.execute("""
            INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (part_id, msg_id_2, session_id, int(time.time() * 1000) + 500, int(time.time() * 1000) + 500,
              '{"type":"tool","tool":"record_event","callID":"call_123","state":{"status":"completed","input":{"type":"observation","symbol":"calculate_banana_tax","payload":{"finding":"quantum tax is 42"}}}}'))
    
# Step 3: Give monitor time to capture the new data
        time.sleep(5)
    
    # Check service status after capture
    status = adapter.get_monitoring_status(temp_project)
    print(f"Service status after capture: {status}", flush=True)
    
    # Step 4: Verify TRACE captured the session
    trace_db = project_mod.db_path(temp_project)
    with sqlite3.connect(trace_db) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM transcript_messages WHERE session_id = ?", (session_id,))
        rows = cursor.fetchall()
        print(f"Transcript messages for session {session_id}: {len(rows)}", flush=True)
        for row in rows:
            print(f"  {row}", flush=True)
        # Should have captured user message, assistant response, and tool call
        assert len(rows) >= 2, f"Expected at least 2 transcript messages, got {len(rows)}"
        # Check for user message
        user_msgs = [r for r in rows if r[2] == "user"]
        assert len(user_msgs) >= 1, "User message not captured"
        assert "quantum_banana" in user_msgs[0][3].lower()
        
        # Check for episodic event (memory tool call)
        cursor.execute("SELECT * FROM events WHERE payload LIKE ?", ('%quantum tax is 42%',))
        events = cursor.fetchall()
        assert len(events) >= 1, "Memory tool event not captured"
    
    # Step 5: Simulate monitor restart (user restarts OpenCode)
    # Stop and restart monitor
    adapter.stop_monitoring(temp_project)
    adapter = create_opencode_transparent_adapter()
    adapter.start_monitoring(temp_project)
    time.sleep(1)
    
    # Step 6: Simulate Session 2 - New OpenCode session
    session_id_2 = f"ses_e2e_test_session_2_{int(time.time() * 1000)}"
    with sqlite3.connect(opencode_db) as conn:
        cursor = conn.cursor()
        # Insert session using dict-based insert to avoid column count issues
        session_data_2 = {
            'id': session_id_2,
            'project_id': 'test_project',
            'workspace_id': '',
            'parent_id': '',
            'slug': 'e2e-test-session-2',
            'directory': str(temp_project),
            'path': '',
            'title': 'E2E Test Session 2 - Continue',
            'version': '1',
            'share_url': '',
            'summary_additions': 0,
            'summary_deletions': 0,
            'summary_files': 0,
            'summary_diffs': '',
            'metadata': '{}',
            'cost': 0.0,
            'tokens_input': 0,
            'tokens_output': 0,
            'tokens_reasoning': 0,
            'tokens_cache_read': 0,
            'tokens_cache_write': 0,
            'revert': '',
            'permission': '',
            'agent': '',
            'model': '',
            'time_created': int(time.time() * 1000) + 10000,
            'time_updated': int(time.time() * 1000) + 10000,
            'time_compacting': 0,
            'time_archived': 0,
        }
        cols = ', '.join(session_data_2.keys())
        placeholders = ', '.join(['?' for _ in session_data_2])
        cursor.execute(f"INSERT INTO session ({cols}) VALUES ({placeholders})", list(session_data_2.values()))
        
        # Insert user asking to continue
        msg_id_3 = f"msg_e2e_user_2_{int(time.time() * 1000)}"
        cursor.execute("""
            INSERT INTO message (id, session_id, time_created, time_updated, data)
            VALUES (?, ?, ?, ?, ?)
        """, (msg_id_3, session_id_2, int(time.time() * 1000) + 10000, int(time.time() * 1000) + 10000,
              '{"role":"user","content":"Continue the quantum banana work. What was the tax calculation?"}'))
    
    # Step 7: Give monitor time to capture
    time.sleep(5)
    
    # Step 8: Verify retrieval works - new session can access previous context
    # Use the adapter's context retrieval
    adapter2 = create_opencode_transparent_adapter()
    context = adapter2.get_recent_context(temp_project, "quantum banana", limit=10)
    assert len(context) >= 1, "Previous context not retrievable"
    assert any("quantum" in c.content.lower() for c in context), "Quantum banana context not found"
    assert any("tax" in c.content.lower() for c in context), "Tax calculation context not found"
    
    # Step 9: Verify MCP retrieval would work (via transcript search)
    transcript_sessions = project_mod.list_transcript_sessions(temp_project, limit=5)
    session_ids = [s["session_id"] for s in transcript_sessions]
    assert session_id in session_ids, "Session 1 not in transcript sessions"
    
    # Get full session transcript
    session_transcript = project_mod.get_transcript_session(temp_project, session_id, limit=50)
    assert len(session_transcript) >= 2, "Full transcript not available"
    
    # Verify the key finding is present
    tax_findings = [m for m in session_transcript if "tax" in m["content"].lower()]
    assert len(tax_findings) >= 1, "Tax finding not in transcript"
    
    # Cleanup
    adapter.stop_monitoring(temp_project)
    adapter2.stop_monitoring(temp_project)
    # Give extra time for subprocess to fully terminate and release DB locks
    time.sleep(5.0)
    import gc
    gc.collect()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])