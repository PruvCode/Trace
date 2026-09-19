import sqlite3
import time

conn = sqlite3.connect(r'C:\Users\pruth\.local\share\opencode\opencode.db')
cursor = conn.cursor()
cursor.execute("SELECT id, title, directory, time_created FROM session WHERE directory LIKE '%transparent-test%' ORDER BY time_created DESC LIMIT 5")
for row in cursor.fetchall():
    print(f'id={row[0]}, title={row[1]}, dir={row[2]}, time={row[3]}')

# Current time in milliseconds
print(f"Current time (ms): {int(time.time() * 1000)}")