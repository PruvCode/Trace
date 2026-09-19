import sqlite3
import time
from pathlib import Path

opencode_db = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
session_id = "ses_manual_123"

with sqlite3.connect(opencode_db) as conn:
    cursor = conn.cursor()
    # Insert session
    cursor.execute("""
        INSERT INTO session (id, project_id, workspace_id, parent_id, slug, directory, path, title, version, share_url,
            summary_additions, summary_deletions, summary_files, summary_diffs, metadata, cost, tokens_input,
            tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, revert, permission, agent, model,
            time_created, time_updated, time_compacting, time_archived)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ('ses_manual_123', 'test_project', '', '', 'manual-test', 'C:/Users/pruth/AppData/Local/Temp/test-final2', '', 'Manual Test', '1', '', 0, 0, 0, '', '{}', 0.0, 0, 0, 0, 0, 0, '', '', '', '', int(time.time()*1000), int(time.time()*1000), 0, 0))
    
    # Insert user message
    cursor.execute('INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)',
        ('msg_1', 'ses_manual_123', int(time.time()*1000), int(time.time()*1000), '{"role":"user","content":"Test message"}'))
    
    # Insert assistant message
    cursor.execute('INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)',
        ('msg_2', 'ses_manual_123', int(time.time()*1000)+1000, int(time.time()*1000)+1000, '{"role":"assistant","content":"Response"}'))
    
    # Insert tool call part
    cursor.execute('INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)',
        ('part_1', 'msg_2', 'ses_manual_123', int(time.time()*1000)+500, int(time.time()*1000)+500, '{"type":"tool","tool":"record_event","callID":"call_1","state":{"status":"completed","input":{"type":"observation","symbol":"test_symbol","payload":{"finding":"test finding"}}}}'))

print('Inserted test data')