import subprocess
result = subprocess.run(['C:\\Users\\pruth\\.venv\\Scripts\\python.exe', '-m', 'py_compile', 'tests/memory/test_secret_redaction.py'], capture_output=True, text=True)
print(result.stdout)
print(result.stderr)