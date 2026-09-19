import time
import threading
from pathlib import Path
from memory.opencode_transparent import create_opencode_transparent_adapter

# Create adapter
adapter = create_opencode_transparent_adapter()

# Start monitoring
project_path = Path(r"C:\Users\pruth\AppData\Local\Temp\transparent-test")
result = adapter.start_monitoring(project_path)
print(f"Start result: {result}")

# Check status immediately
status = adapter.get_monitoring_status()
print(f"Immediate status: {status}")

# Wait a bit
print("Waiting 3 seconds...")
time.sleep(3)

# Check status again
status = adapter.get_monitoring_status()
print(f"Status after 3s: {status}")

# Check if thread is alive
if adapter._monitor and adapter._monitor._thread:
    print(f"Thread alive: {adapter._monitor._thread.is_alive()}")

# Keep running for a bit more
print("Waiting 5 more seconds...")
time.sleep(5)

status = adapter.get_monitoring_status()
print(f"Status after 8s: {status}")

# Stop monitoring
result = adapter.stop_monitoring()
print(f"Stop result: {result}")

status = adapter.get_monitoring_status()
print(f"Final status: {status}")