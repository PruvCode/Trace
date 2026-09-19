import sqlite3

conn = sqlite3.connect(r'C:\Users\pruth\.local\share\opencode\opencode.db')
cursor = conn.cursor()

# Find text parts (likely user/assistant messages)
cursor.execute("SELECT data FROM part WHERE json_extract(data, '$.type') = 'text' ORDER BY time_created DESC LIMIT 10")
for row in cursor.fetchall():
    print(row[0][:500])
    print()