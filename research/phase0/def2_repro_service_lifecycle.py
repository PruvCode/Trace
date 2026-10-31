"""DEF-2 focused repro (bounded, model-free).

Reproduces the two probe symptoms deterministically, then classifies:

  S1  setup_integration returns monitoring:"error" on a fresh home where
      OpenCode has never run (OpenCode DB absent) - precondition message
      expected verbatim; then with the `opencode models` primer (creates the
      DB) the same call must return "started".
  S2  capture_once returns {"status": "busy", "message": "a monitor is
      already capturing"} while a monitor is live (the plugin's
      ensure-capture starts one), and succeeds after stop().

Everything under %TEMP%\\trace_recon\\def2_repro; no user stores touched.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

WT = Path(r"C:\Users\pruth\trace-glm")
sys.path.insert(0, str(WT))

OPENCODE = Path(os.environ["TEMP"]) / "trace_tools" / "oc118" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
ROOT = Path(os.environ["TEMP"]) / "trace_recon" / "def2_repro"
if ROOT.exists():
    shutil.rmtree(ROOT)

home = ROOT / "home"          # isolated XDG home
ws = ROOT / "proj"            # "project" workspace
ws.mkdir(parents=True)

env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
for name in ("data", "config", "cache", "state"):
    (home / name).mkdir(parents=True, exist_ok=True)
    env[f"XDG_{name.upper()}_HOME"] = str(home / name)
env["PYTHONPATH"] = str(WT)

SETUP = ("import json,sys; from pathlib import Path; from trace_memory import project as p;"
         "from memory.opencode_transparent import create_opencode_transparent_adapter as c;"
         "d=Path(sys.argv[1]); p.init_project(d); r=c().setup_integration(d);"
         "print(json.dumps({'setup': r['setup'], 'monitoring': r['monitoring']}))")


def run(code: str, *args: str, timeout: int = 240) -> dict:
    p = subprocess.run([sys.executable, "-c", code, str(ws), *args],
                       cwd=str(ws), env=env, capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": (p.stderr or p.stdout or "")[-300:]}


oc_db = home / "data" / "opencode" / "opencode.db"

# --- S1: setup BEFORE any OpenCode run --------------------------------------
print("opencode db exists before primer:", oc_db.exists())
r1 = run(SETUP)
print("S1 setup (no db):", json.dumps(r1))

# --- primer (Claude's harness step my probe lacked): creates the DB ----------
pr = subprocess.run([str(OPENCODE), "models"], env=env, capture_output=True,
                    text=True, timeout=300, cwd=str(ws))
print("primer rc:", pr.returncode, "| db exists now:", oc_db.exists())

r2 = run(SETUP)
print("S1 setup (after primer):", json.dumps(r2))

# --- S2: capture_once is busy while the monitor runs; ok after stop ----------
FLUSH = ("import json,sys; from pathlib import Path;"
         "from memory.opencode_service import get_service;"
         "s=get_service(Path(sys.argv[1]));"
         "print(json.dumps({'capture_once': s.capture_once(), 'living': s.is_running(),"
         "                  'stop': s.stop()}))")
r3 = run(FLUSH)
print("S2 flush while monitor live:", json.dumps(r3))

time.sleep(0.5)
r4 = run(FLUSH)
print("S2 flush after stop:", json.dumps(r4))

# Service leftovers? (sandbox only: children we own)
print("done")
