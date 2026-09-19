import sqlite3

conn = sqlite3.connect(r'C:\Users\pruth\.local\share\opencode\opencode.db')
cursor = conn.cursor()

# Find all distinct part types
cursor.execute("SELECT DISTINCT json_extract(data, '$.type') FROM part WHERE json_type(data) = 'object'")
types = cursor.fetchall()
for t in types:
    print(t[0])