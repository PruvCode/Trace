import sqlite3

conn = sqlite3.connect(r'C:\Users\pruth\.local\share\opencode\opencode.db')
cursor = conn.cursor()
cursor.execute('SELECT name FROM sqlite_master WHERE type="table"')
tables = cursor.fetchall()
for t in tables:
    print(t[0])
    cursor.execute(f'PRAGMA table_info({t[0]})')
    for col in cursor.fetchall():
        print(f'  {col}')