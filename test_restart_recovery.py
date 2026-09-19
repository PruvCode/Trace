"""Test monitor restart recovery - stop monitor, create new OpenCode session, restart monitor and verify catch-up."""
import time
from pathlib import Path
from memory.opencode_transparent import create_opencode_transparent_adapter
import sqlite3

# Create adapter
adapter = create_opencode_transparent_adapter()

# Start monitoring
project_path = Path(r"C:\Users\pruth\AppData\Local\Temp\transparent-test")
result = adapter.start_monitoring(project_path)
print(f"Start result: {result}")

# Wait a bit
print("Waiting 2 seconds...")
time.sleep(2)

# Check status before stop
status = adapter.get_monitoring_status()
print(f"Status before stop: {status}")

# Stop monitoring
result = adapter.stop_monitoring()
print(f"Stop result: {result}")

# Now simulate a new OpenCode session by directly inserting into OpenCode's database
opencode_db = Path(r"C:\Users\pruth\.local\share\opencode\opencode.db")
new_session_id = "ses_test_restart_recovery_123"

# Insert test data with explicit column mapping
with sqlite3.connect(opencode_db) as conn:
    cursor = conn.cursor()
    
    # Session table has 29 columns - use explicit dict-based insert
    session_data = {
        'id': new_session_id,
        'project_id': 'test_project',
        'workspace_id': '',
        'parent_id': '',
        'slug': 'test-restart-recovery',
        'directory': 'C:/Users/pruth/AppData/Local/Temp/transparent-test',
        'path': '',
        'title': 'Test restart recovery',
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
    cursor = conn.cursor()
    cursor.execute(f"INSERT INTO session ({cols}) VALUES ({placeholders})", list(session_data.values()))
    
    # Insert a test message
    msg_id = "msg_test_restart_123"
    cursor.execute("""
        INSERT INTO message (id, session_id, time_created, time_updated, data)
        VALUES (?, ?, ?, ?, ?)
    """, (msg_id, new_session_id, int(time.time() * 1000), int(time.time() * 1000), '{"role":"user","content":"test"}'))
    
    # Insert a test part
    part_id = "prt_test_restart_123"
    cursor.execute("""
        INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (part_id, msg_id, new_session_id, int(time.time() * 1000), int(time.time() * 1000), 
          '{"type":"text","text":"Test restart recovery message"}'))

print(f"Inserted test session {new_session_id}")

# Now restart the monitor
print("Restarting monitor...")
adapter = create_opencode_transparent_adapter()
result = adapter.start_monitoring(Path(r"C:\Users\pruth\AppData\Local\Temp\transparent-test"))
print(f"Restart result: {result}")

# Wait for monitor to process
print("Waiting 3 seconds for monitor to catch up...")
time.sleep(3)

# Check status
status = adapter.get_monitoring_status()
print(f"Status after restart: {status}")

# Check if the new session was captured
trace_db = Path(r"C:\Users\pruth\AppData\Local\Temp\transparent-test\.agent-memory\memory.db")
with sqlite3.connect(trace_db) as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM transcript_messages WHERE session_id = ?", (new_session_id,))
    rows = cursor.fetchall()
    print(f"Transcript messages for new session: {len(rows)}")
    for row in rows:
        print(f"  {row}")

# Stop monitor
adapter = create_opencode_transparent_adapter()
result = adapter.stop_monitoring()
print(f"Stop result: {result}")