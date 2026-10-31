"""Two-session TRACE probe against an OpenCode binary, fully isolated from the user's data.

Session 1 states a fact; session 2 must recall it. Reports capture, injection,
recall, and whether the plugin hook fired. For 2.x pass --standalone so the run
uses a private server instead of the background service.

Usage: python research/phase3/compat_probe.py <opencode-binary> [--standalone]
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BIN = sys.argv[1]
EXTRA = ["--standalone"] if "--standalone" in sys.argv else []
MODEL = "opencode/big-pickle"
SETUP = ("import sys; from pathlib import Path; from trace_memory import project as p;"
         "from memory.opencode_transparent import create_opencode_transparent_adapter as c;"
         "d=Path(sys.argv[1]); p.init_project(d); r=c().setup_integration(d);"
         "print({k: r[k].get('status') for k in ('setup','plugin','monitoring')})")
STOP = "import sys; from pathlib import Path; from memory.opencode_service import get_service as g; print(g(Path(sys.argv[1])).stop())"

root = Path(tempfile.mkdtemp(prefix="trace_compat_"))
ws = root / "project"
ws.mkdir()
(ws / "a.py").write_text("x = 1\n")
env = {k: v for k, v in os.environ.items() if k not in ("PWD", "PYTHONPATH")}
for var, sub in (("USERPROFILE", "home"), ("HOME", "home"), ("APPDATA", "appdata"), ("LOCALAPPDATA", "local"),
                 ("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
    (root / sub).mkdir(exist_ok=True)
    env[var] = str(root / sub)
env["PWD"] = str(ws)
env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
report: dict = {"binary": BIN, "extra": EXTRA}


def opencode(prompt: str) -> dict:
    t = time.time()
    try:
        p = subprocess.run([BIN, "run", *EXTRA, "--format", "json", "-m", MODEL, *([] if EXTRA else ["--dir", str(ws)]), prompt],
                           cwd=ws, env=env, capture_output=True, timeout=300, stdin=subprocess.DEVNULL)
        out, rc = p.stdout, p.returncode
        err = p.stderr.decode("utf-8", "replace")[-300:]
    except subprocess.TimeoutExpired as e:
        out, rc, err = e.stdout or b"", "timeout", ""
    text = []
    tools = sorted(set(re.findall(r"trace-memory[_\w]*", out.decode("utf-8", "replace"))))
    for line in out.decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e.get("part"), dict) and e["part"].get("text"):
            text.append(e["part"]["text"])
    return {"rc": rc, "seconds": round(time.time() - t, 1), "text": " ".join(text)[:160], "tools": tools, "stderr": err if rc else ""}


version = subprocess.run([BIN, "--version"], env=env, capture_output=True, text=True, timeout=60).stdout.strip()
report["version"] = version
report["models"] = subprocess.run([BIN, "models", *EXTRA], env=env, capture_output=True, text=True, timeout=300).returncode
report["setup"] = subprocess.run([sys.executable, "-c", SETUP, str(ws)], cwd=ws, env=env, capture_output=True, text=True, timeout=120).stdout.strip()[:200]
report["first"] = opencode("My project codename is ZEPHYR-41. Acknowledge briefly.")
time.sleep(4)
report["second"] = opencode("What is my project codename? Answer from memory, do not search files.")
report["recalled"] = "ZEPHYR-41" in report["second"]["text"]
third = opencode("Call the trace-memory search_transcripts tool with query ZEPHYR and tell me how many results it returned.")
report["mcp"] = third
log = ws / ".agent-memory" / "retrieval.log"
rows = [json.loads(x) for x in log.read_text().splitlines() if x.strip()] if log.exists() else []
report["hook_rows"] = len(rows)
report["injected_rows"] = sum(1 for r in rows if r.get("injected"))
report["last_hook"] = {k: rows[-1].get(k) for k in ("spawned", "injected", "items", "reason", "error")} if rows else None
subprocess.run([sys.executable, "-c", STOP, str(ws)], cwd=ws, env=env, capture_output=True, timeout=60)
conn = sqlite3.connect(f"file:{ws / '.agent-memory' / 'memory.db'}?mode=ro", uri=True)
report["captured_rows"] = conn.execute("SELECT COUNT(*) FROM transcript_messages").fetchone()[0]
report["fact_captured"] = bool(conn.execute("SELECT 1 FROM transcript_messages WHERE content LIKE '%ZEPHYR-41%'").fetchone())
conn.close()
dbs = [str(p.relative_to(root)) for p in root.rglob("*.db") if ".agent-memory" not in str(p)]
report["opencode_dbs"] = dbs
for db in root.rglob("opencode*.db"):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    report["tables"] = sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))[:40]
    c.close()
    break
service_log = ws / ".trace-service" / "service.log"
report["service_log_tail"] = service_log.read_text(errors="replace")[-400:] if service_log.exists() else None
report["root"] = str(root)
print(json.dumps(report, indent=1))
