import sqlite3

conn = sqlite3.connect(r'C:\Users\pruth\AppData\Local\Temp\transparent-test\.agent-memory\memory.db')
cursor = conn.cursor()
cursor.execute('SELECT name FROM sqlite_master WHERE type="table"')
print([t[0] for t in cursor.fetchall()])
cursor.execute('SELECT COUNT(*) FROM transcript_messages')
print(f'transcript_messages count: {cursor.fetchone()[0]}')