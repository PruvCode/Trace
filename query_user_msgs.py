import sqlite3

conn = sqlite3.connect(r'C:\Users\pruth\.local\share\opencode\opencode.db')
cursor = conn.cursor()

# Find user messages
cursor.execute("SELECT data FROM part WHERE data LIKE '%\"type\":\"user\"%' ORDER BY time_created DESC LIMIT 5")
for row in cursor.fetchall():
    print(row[0][:500])
    print()