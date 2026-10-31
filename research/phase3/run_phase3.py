"""Phase 3: held-out multi-session comparison. Design: docs/engineering-audit/PHASE3_PREREGISTRATION.md

Each task gets a five-session history: session 1 states a fact the code cannot
reveal, sessions 2-5 do unrelated reading. Session B then needs the fact.
Prints one JSON line per run.

Usage: python research/phase3/run_phase3.py <root> <reps> <workers> [tasks...]
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WT = Path(__file__).resolve().parents[2]
PY = sys.executable
OPENCODE = Path(os.environ.get("TRACE_OPENCODE_BIN") or Path(os.environ["TEMP"]) / "trace_tools" / "oc2x" / "node_modules"
                / "@opencode" / "cli" / "bin" / "opencode.exe")
MODEL = "opencode/big-pickle"
ARMS = ["N0", "N1", "T1", "T2"]
HANDOFF = ("\n\nBefore you finish, append a short handoff for future sessions to AGENTS.md in the project root: "
           "what you learned, any decisions or constraints you were told, and anything that failed.")
LOCK = threading.Lock()

NOISE = [
    "Read {file} and summarize it in one sentence. Do not change code.",
    "What does the main function in {file} return? Do not change code.",
    "Are there any obvious typos in the comments of {file}? Do not change code.",
    "List the imports used by {file}. Do not change code.",
]

TASKS = {
    "h1_retry_backoff": dict(
        fixture="net_repo", file="net/retry.py", marker="0.25",
        fact=("Decision from the platform team for later: run_with_retries must make 3 attempts in total and "
              "time.sleep between them, 0.25 seconds first, doubling each time. Just read net/retry.py and say "
              "where that would go. Do not change code."),
        task="Fix run_with_retries and add the backoff the platform team decided on.",
        check=("import net.retry as r, time\ncalls=[]\ntime.sleep=calls.append\nn=[]\n"
               "def f():\n    n.append(1)\n    raise ValueError('x')\n"
               "try:\n    r.run_with_retries(f)\nexcept ValueError:\n    pass\nassert len(n)==3 and calls==[0.25,0.5], (n,calls)\n")),
    "h2_exporter_delimiter": dict(
        fixture="data_repo", file="ingest/csv_loader.py", marker="|",
        fact=("Note for later: files from the partner exporter separate columns with the pipe character, never "
              "commas, and always have a header row. Just read ingest/csv_loader.py and say how it reads files. "
              "Do not change code."),
        task="Add a keyword argument source to load_rows; source='partner' must read files from the partner exporter correctly. Also skip the header row.",
        check=("import tempfile,os\nfrom ingest.csv_loader import load_rows\nd=tempfile.mkdtemp()\np=os.path.join(d,'x')\n"
               "open(p,'w').write('a|b\\n1|2\\n')\nassert load_rows(p,source='partner')==[['1','2']]\n")),
    "h3_priority_limit": dict(
        fixture="queue_repo", file="api/submit.py", marker="9",
        fact=("Ops agreed today: a job with priority above 9 must get status 'rejected' and never reach the worker. "
              "Just read api/submit.py and explain the flow. Do not change code."),
        task="Fix submit's retry handling and enforce the job priority limit ops agreed on.",
        check=("from api.submit import submit\nassert submit({'priority':10,'payload':1})=={'status':'rejected'}\n"
               "assert submit({'priority':9,'payload':1})['status']=='ok'\nassert submit({'fail':True})=={'status':'retry'}\n")),
    "h4_partner_contract": dict(
        fixture="api_repo", file="api/serialize.py", marker="display_name",
        fact=("The partner signed off the wire contract: the user name field is called display_name and every "
              "payload carries schema_version 3. Just read api/serialize.py and say what it emits. Do not change code."),
        task="Bring to_wire in line with the wire contract the partner signed off. Keep the user id field as it is.",
        check=("from api.serialize import to_wire\nw=to_wire({'user_id':'u','name':'n'})\n"
               "assert w['display_name']=='n' and w['schema_version']==3 and w['user_id']=='u', w\n")),
    "h5_compliance_limit": dict(
        fixture="profiles_repo", file="app/security.py", marker="120",
        fact=("Compliance ticket SEC-118: sanitized values must be cut to the first 120 characters after stripping. "
              "Just read app/security.py and describe it. Do not change code."),
        task="Apply the limit from the compliance ticket to sanitize.",
        check=("from app.security import sanitize\nassert sanitize('<b>'+'x'*200+'</b>')=='x'*120\nassert sanitize(' ok ')=='ok'\n")),
    "h6_legal_hold": dict(
        fixture="store_repo", file="migrate/backfill.py", marker="legal_hold",
        fact=("Legal told us: any row with a truthy legal_hold key must never be archived or put in the ledger. "
              "Just read migrate/backfill.py and summarize it. Do not change code."),
        task="Update run_backfill so it follows what legal told us about held rows.",
        check=("from migrate.backfill import run_backfill\nr=[{'id':1,'legal_hold':True},{'id':2}]\nl=[]\nrun_backfill(r,l)\n"
               "assert 'archived' not in r[0] and r[1]['archived'] and [e['row'] for e in l]==[2]\n")),
}

SETUP = ("import json,sys; from pathlib import Path; from trace_memory import project as p;"
         "from memory.opencode_transparent import create_opencode_transparent_adapter as c;"
         "d=Path(sys.argv[1]); p.init_project(d); r=c().setup_integration(d);"
         "print(json.dumps({'setup': r['setup']['status'], 'monitoring': r['monitoring']['status']}))")
STOP = ("import json,sys; from pathlib import Path; from memory.opencode_service import get_service;"
        "s=get_service(Path(sys.argv[1])); s.stop(); print(json.dumps({'capture': str(s.capture_once())[:120]}))")


def log(obj: dict) -> None:
    with LOCK:
        print(json.dumps(obj), flush=True)


def env_for(home: Path, ws: Path, extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PWD", "PYTHONPATH")}
    for var, sub in (("USERPROFILE", "home"), ("HOME", "home"), ("APPDATA", "appdata"), ("LOCALAPPDATA", "local"),
                     ("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
        (home / sub).mkdir(parents=True, exist_ok=True)
        env[var] = str(home / sub)
    env["PWD"] = str(ws)
    env["PYTHONPATH"] = str(WT)
    env.update(extra or {})
    return env


def workspace(spec: dict, ws: Path) -> None:
    shutil.copytree(WT / "fixtures" / spec["fixture"], ws, ignore=shutil.ignore_patterns(".git", ".agent-memory", "__pycache__"))
    for path in ws.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        path.write_text(re.sub(r"[ \t]*#\s*BUG\b.*$", "", text, flags=re.M), encoding="utf-8")


def opencode(ws: Path, env: dict, prompt: str) -> dict:
    t = time.time()
    try:
        p = subprocess.run([str(OPENCODE), "run", "--standalone", "--format", "json", "-m", MODEL, prompt],
                           cwd=ws, env=env, capture_output=True, timeout=420, stdin=subprocess.DEVNULL)
        out, rc = p.stdout, p.returncode
    except subprocess.TimeoutExpired as e:
        out, rc = e.stdout or b"", "timeout"
    text = out.decode("utf-8", "replace")
    tools = sum(1 for x in text.splitlines() if '"type":"tool' in x)
    return {"rc": rc, "seconds": round(time.time() - t, 1), "tools": tools, "memory_tools": len(re.findall(r'"tool":"trace-memory', text)),
            "session": (re.search(r'"sessionID":"(ses_[^"]+)"', text) or [None, None])[1]}


def tokens(home: Path, sid: str | None) -> dict:
    db = home / "data" / "opencode" / "opencode.db"
    if not sid or not db.exists():
        return {"input": 0, "cache_read": 0}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = c.execute("SELECT tokens_input, tokens_cache_read FROM session_v2 WHERE id=?", (sid,)).fetchone() or (0, 0)
    c.close()
    return {"input": row[0] or 0, "cache_read": row[1] or 0}


def evaluate(spec: dict, ws: Path) -> bool:
    try:
        return subprocess.run([PY, "-c", spec["check"]], cwd=ws, env={**os.environ, "PYTHONPATH": str(ws)},
                              capture_output=True, timeout=60).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def py(code: str, ws: Path, env: dict) -> str:
    return subprocess.run([PY, "-c", code, str(ws)], cwd=ws, env=env, capture_output=True, text=True, timeout=120).stdout.strip()[-200:]


def prime(env: dict) -> None:
    """Create the isolated OpenCode database without touching a background service."""
    subprocess.run([str(OPENCODE), "models", "--standalone"], env=env, capture_output=True, timeout=300)


def escaped(home: Path, sid: str | None, ws: Path) -> bool:
    """Did the agent look outside its workspace? Such runs could find history directly."""
    db = home / "data" / "opencode" / "opencode.db"
    if not sid or not db.exists():
        return False
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    text = "
".join(r[0] for r in c.execute("SELECT data FROM session_message WHERE session_id=? AND type='assistant'", (sid,)))
    c.close()
    calls = [json.dumps(p.get("input") or p) for m in text.splitlines() for p in json.loads(m).get("content", [])
             if isinstance(p, dict) and p.get("type") == "tool"]
    outside = re.compile(r"\.\.[/\]|trace_p3_h|AppData\\Local\\Temp(?!.*" + re.escape(ws.parent.name) + ")")
    return any(outside.search(c) for c in calls)


def history(name: str, spec: dict, base: Path, handoff: bool) -> tuple[Path, Path, str]:
    ws, home = base / "proj", base / "home"
    workspace(spec, ws)
    env = env_for(home, ws)
    prime(env)
    setup = py(SETUP, ws, env)
    prompts = [spec["fact"], *(p.format(file=spec["file"]) for p in NOISE)]
    results = [opencode(ws, env, p + (HANDOFF if handoff else "")) for p in prompts]
    flush = py(STOP, ws, env)
    agents = ws / "AGENTS.md"
    notes = agents.read_text(encoding="utf-8") if agents.exists() else ""
    conn = sqlite3.connect(f"file:{ws / '.agent-memory' / 'memory.db'}?mode=ro", uri=True)
    stored = "\n".join(r[0] or "" for r in conn.execute("SELECT content FROM transcript_messages"))
    conn.close()
    log({"phase": "history", "task": name, "handoff": handoff, "rcs": [r["rc"] for r in results], "setup": setup, "flush": flush,
         "notes_chars": len(notes), "notes_has_fact": spec["marker"] in notes, "store_has_fact": spec["marker"] in stored})
    return ws, home, notes


def session_b(name: str, spec: dict, arm: str, rep: int, root: Path, a_ws: Path, a_home: Path, notes: str) -> None:
    # Nothing from history may be reachable: a run in a sibling folder read it directly.
    ws = Path(tempfile.mkdtemp(prefix="p3b_", dir=root.parent.parent / "trace_p3_b")) / "proj"
    home = Path(tempfile.mkdtemp(prefix="p3h_", dir=root.parent.parent / "trace_p3_b_h"))
    workspace(spec, ws)
    extra = {}
    if arm.startswith("N1") and notes:
        (ws / "AGENTS.md").write_text(notes, encoding="utf-8")
    if arm.startswith("T"):
        shutil.copytree(a_ws / ".agent-memory", ws / ".agent-memory")
        extra["TRACE_TASK_AWARE"] = "0" if arm == "T1" else "1"
    env = env_for(home, ws, extra)
    prime(env)
    if arm.startswith("T"):
        py(SETUP, ws, env)
        if arm == "T1":  # previous default: every MCP tool
            cfg = ws / "opencode.json"
            data = json.loads(cfg.read_text(encoding="utf-8"))
            data["mcp"]["trace-memory"]["command"] = data["mcp"]["trace-memory"]["command"][:-2]
            cfg.write_text(json.dumps(data), encoding="utf-8")
    b = opencode(ws, env, spec["task"])
    if arm.startswith("T"):
        py("import sys; from pathlib import Path; from memory.opencode_service import get_service as g; g(Path(sys.argv[1])).stop()", ws, env)
    rlog = ws / ".agent-memory" / "retrieval.log"
    rows = [json.loads(x) for x in rlog.read_text(encoding="utf-8").splitlines() if x.strip()] if rlog.exists() else []
    rows = [r for r in rows if r.get("sessionID") == b["session"]]
    log({"phase": "B", "task": name, "arm": arm, "rep": rep, **b, "pass": evaluate(spec, ws), "escaped": escaped(home, b["session"], ws), "tokens": tokens(home, b["session"]),
         "requests": len(rows), "injected_requests": sum(1 for r in rows if r.get("injected")),
         "injected_chars": max([r.get("chars", 0) for r in rows] or [0])})


def run_task(name: str, root: Path, reps: int) -> None:
    spec = TASKS[name]
    tdir = root / name
    a_ws, a_home, notes = history(name, spec, tdir / "H", True)
    rng = random.Random(f"20261031:{name}")
    for rep in range(reps):
        order = ARMS[:]
        rng.shuffle(order)
        for arm in order:
            session_b(name, spec, arm, rep, tdir, a_ws, a_home, notes)
    # Secondary: nobody asked for notes.
    u_ws, u_home, u_notes = history(name, spec, tdir / "U", False)
    for rep in range(reps):
        session_b(name, spec, "T2U", rep, tdir, u_ws, u_home, u_notes)
        session_b(name, spec, "N1U", rep, tdir, u_ws, u_home, u_notes)


def main() -> None:
    root = Path(sys.argv[1])
    reps, workers = int(sys.argv[2]), int(sys.argv[3])
    names = sys.argv[4:] or list(TASKS)
    root.mkdir(parents=True, exist_ok=True)
    for name in ("trace_p3_b", "trace_p3_b_h"):
        (root.parent / name).mkdir(exist_ok=True)
    log({"phase": "start", "model": MODEL, "opencode": str(OPENCODE.name), "version": subprocess.run(
        [str(OPENCODE), "--version"], capture_output=True, text=True, timeout=60).stdout.strip(), "tasks": names, "reps": reps,
         "head": subprocess.run(["git", "-C", str(WT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()})
    with ThreadPoolExecutor(workers) as pool:
        for f in [pool.submit(run_task, n, root, reps) for n in names]:
            try:
                f.result()
            except Exception as e:  # one broken task must not stop the others
                log({"phase": "task_error", "error": f"{type(e).__name__}: {e}"[:300]})


if __name__ == "__main__":
    main()
