"""Phase 0: independent A/B probe of the injection policy (D3 claim).

Verifies, on a disposable OpenCode 1.18.32 + free-tier model:

  policy A  once-per-session injection (recovery-branch baseline plugin)
  policy B  every-request injection (autonomous workspace baedd4b plugin)

Per policy trial:
  1. Session A (earlier) seeds a store fact (token-format v2 decision)
     through a real OpenCode run with TRACE capture, then flushes capture.
  2. Session B (fresh fixture workspace, store copied from A) must (i) run a
     tool call and (ii) afterwards write a recall answer file from memory
     only. The recall therefore happens on a LATER model request - the exact
     scenario that motivated the per-request revert.

Evidence recorded per session (content-free):
  - retrieval.log rows: requests, injected count, injected chars
  - recall pass (deterministic on-disk check of recall_answer.md)
  - whether the synthetic block's header marker appears anywhere in the
    OpenCode message store (persistence probe)
  - steps/tools/seconds/tokens of the OpenCode run

Usage:
  python research/phase0/probe_injection_policy.py OUT_DIR POLICY TRIALS [TASK]

POLICY is "A" (once-per-session) or "B" (per-request). The script never
reads or writes user session data outside the sandbox; it uses only the
synthetic task defined below.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

WT = Path(__file__).resolve().parents[2]
PY = sys.executable
OPENCODE = Path(os.environ["TEMP"]) / "trace_tools" / "oc118" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
MODEL = "opencode/big-pickle"
LOCK = threading.Lock()

# Stable header of the memory block for the persistence probe (content-free).
BLOCK_MARKER = "[TRACE PROJECT MEMORY - RECOVERED FROM A PREVIOUS SESSION]"


TASK = "C_episodic/task_02_decision"
TASK_SPEC = dict(
    facts=["v2:"],
    a=("Heads-up from the platform team about an upcoming change: token format v2. New tokens carry a `v2:` "
       "version prefix in front of the normal token, e.g. `v2:token-abc`. When we implement it, validate_token "
       "must accept both legacy `token-...` and `v2:token-...` tokens and reject everything else. For now just "
       "read auth/tokens.py and tell me where that change would go. Do not change any code."),
)
HANDOFF = ("\n\nBefore you finish, append a short handoff for future sessions to AGENTS.md in the project root: "
           "what you learned, any decisions or constraints you were told, and anything that failed.")

B_PROMPT = ("Step 1: read auth/tokens.py (a single tool call is enough for step 1). Then step 3, WITHOUT reading "
            "any other file and using only your memory of prior sessions, create the file recall_answer.md in "
            "the project root containing exactly one line of the form "
            "EXAMPLE=<the exact example token format given in the earlier platform decision>. "
            "Change no other file.")


def log(obj: dict) -> None:
    with LOCK:
        print(json.dumps(obj), flush=True)


def env_for(home: Path, extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PWD",)}
    for name in ("data", "config", "cache", "state"):
        (home / name).mkdir(parents=True, exist_ok=True)
        env[f"XDG_{name.upper()}_HOME"] = str(home / name)
    env["PYTHONPATH"] = str(WT)
    env["TRACE_TASK_AWARE"] = "0"  # C2-style recency block for BOTH policies
    env.update(extra or {})
    return env


def make_workspace(task: dict, ws: Path) -> None:
    raw = yaml.safe_load((WT / "tasks" / task / "task.yaml").read_text(encoding="utf-8"))
    fixture = WT / raw["fixture"]
    blob = subprocess.run(["git", "-C", str(fixture), "archive", raw["base_commit"]],
                          capture_output=True, check=True).stdout
    ws.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extractall(ws, filter="data")
    for path in ws.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        path.write_text(re.sub(r"[ \t]*#\s*BUG\b.*$", "", text, flags=re.M), encoding="utf-8")


def trace_py(code: str, ws: Path, env: dict, *args: str, timeout: int = 300) -> dict:
    p = subprocess.run([PY, "-c", code, str(ws), *args], cwd=ws, env=env,
                       capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": (p.stderr or "")[-400:]}


SETUP = ("import json,sys; from pathlib import Path; from trace_memory import project as p;"
         "from memory.opencode_transparent import create_opencode_transparent_adapter as c;"
         "d=Path(sys.argv[1]); p.init_project(d); r=c().setup_integration(d);"
         "print(json.dumps({'setup': r['setup']['status'], 'monitoring': r['monitoring']['status']}))")
STOP = ("import json,sys; from pathlib import Path; from memory.opencode_service import get_service;"
        "s=get_service(Path(sys.argv[1])); r=s.capture_once() if len(sys.argv)>2 else None; s.stop();"
        "print(json.dumps({'capture': str(r)[:200]}))")


def run_opencode(ws: Path, env: dict, prompt: str, timeout: int = 900) -> dict:
    t = time.time()
    env = {**env, "PWD": str(ws)}
    try:
        p = subprocess.run([str(OPENCODE), "run", "--format", "json", "-m", MODEL, "--dir", str(ws), prompt],
                           cwd=ws, env=env, capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        out, rc = p.stdout, p.returncode
    except subprocess.TimeoutExpired as e:
        out, rc = (e.stdout or b""), "timeout"
    steps = tools = 0
    sid = None
    for line in out.decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        sid = sid or e.get("sessionID")
        steps += e.get("type") == "step_start"
        tools += e.get("type") == "tool_use"
    return {"rc": rc, "steps": steps, "tools": tools, "seconds": round(time.time() - t, 1), "session": sid}


def tokens(home: Path, sid: str | None) -> dict:
    db = home / "data" / "opencode" / "opencode.db"
    tot = {"input": 0, "output": 0, "cache_read": 0}
    if not sid or not db.exists():
        return tot
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    for (data,) in c.execute("SELECT data FROM message WHERE session_id=?", (sid,)):
        t = (json.loads(data).get("tokens") or {})
        tot["input"] += t.get("input") or 0
        tot["output"] += t.get("output") or 0
        tot["cache_read"] += (t.get("cache") or {}).get("read") or 0
    c.close()
    return tot


def retrieval_rows(ws: Path, sid: str | None) -> dict:
    rlog = ws / ".agent-memory" / "retrieval.log"
    if not rlog.exists():
        return {"requests": 0, "injected_requests": 0, "injected_chars": 0}
    rows = []
    for line in rlog.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if sid and row.get("sessionID") != sid:
            continue
        rows.append(row)
    return {"requests": len(rows),
            "injected_requests": sum(1 for r in rows if r.get("injected")),
            "injected_chars": max([r.get("chars", 0) for r in rows] or [0])}


def block_persisted_in_opencode(home: Path, sid: str | None) -> bool | None:
    """Does the OpenCode message store hold the synthetic block? (D3 premiss)"""
    db = home / "data" / "opencode" / "opencode.db"
    if not sid or not db.exists():
        return None
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for (data,) in c.execute(
                "SELECT data FROM message WHERE session_id=? UNION ALL "
                "SELECT data FROM part WHERE session_id=?", (sid, sid)):
            if BLOCK_MARKER in (data or ""):
                return True
        return False
    finally:
        c.close()


def store_has(ws: Path, facts: list[str]) -> bool | None:
    db = ws / ".agent-memory" / "memory.db"
    if not db.exists():
        return None
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    text = "\n".join(r[0] or "" for r in c.execute("SELECT content FROM transcript_messages"))
    c.close()
    return all(f in text for f in facts)


def recall_pass(ws: Path) -> bool:
    ans = ws / "recall_answer.md"
    if not ans.exists():
        return False
    text = ans.read_text(encoding="utf-8")
    return bool(re.search(r"EXAMPLE\s*=\s*v2:token-[A-Za-z0-9-]+", text))


def run_probe(policy: str, trials: int, root: Path, task: str) -> None:
    spec = TASK_SPEC
    for trial in range(trials):
        tdir = root / f"{policy}_{trial}"
        # ---- Session A: seed the store fact (earlier session) ----
        a_home, a_ws = tdir / "A" / "home", tdir / "A" / "proj"
        make_workspace(task, a_ws)
        a_env = env_for(a_home)
        setup_a = trace_py(SETUP, a_ws, a_env)
        a = run_opencode(a_ws, a_env, spec["a"] + HANDOFF)
        flush_a = trace_py(STOP, a_ws, a_env, "capture")
        a_store_facts = store_has(a_ws, spec["facts"])
        log({"policy": policy, "trial": trial, "session": "A", **a, "setup": setup_a,
             "flush": flush_a, "store_has_facts": a_store_facts,
             "block_persisted": block_persisted_in_opencode(a_home, a["session"])})
        if not a_store_facts:
            log({"policy": policy, "trial": trial, "error": "session A produced no store facts; SKIPPING B"})
            continue

        # ---- Session B: tool call, then from-memory recall on a later request ----
        b_home, b_ws = tdir / "B" / "home", tdir / "B" / "proj"
        make_workspace(task, b_ws)
        shutil.copytree(a_home, b_home)
        shutil.copytree(a_ws / ".agent-memory", b_ws / ".agent-memory")
        b_env = env_for(b_home)
        setup_b = trace_py(SETUP, b_ws, b_env)
        b = run_opencode(b_ws, b_env, B_PROMPT)
        flush_b = trace_py(STOP, b_ws, b_env, "capture")
        rows = retrieval_rows(b_ws, b["session"])
        log({"policy": policy, "trial": trial, "session": "B", **b, "setup": setup_b,
             "flush": flush_b, "recall_pass": recall_pass(b_ws), "retrieval": rows,
             "nums": tokens(b_home, b["session"]),
             "block_persisted": block_persisted_in_opencode(b_home, b["session"]),
             "workspace_head": subprocess.run(["git", "-C", str(WT), "rev-parse", "HEAD"],
                                              capture_output=True, text=True).stdout.strip()})


def main() -> None:
    root = Path(sys.argv[1])
    policy = sys.argv[2].upper()
    trials = int(sys.argv[3])
    root.mkdir(parents=True, exist_ok=True)
    log({"phase": "start", "policy": policy, "trials": trials, "model": MODEL,
         "opencode": str(OPENCODE), "task": TASK, "block_marker": BLOCK_MARKER,
         "worktree_head": subprocess.run(["git", "-C", str(WT), "rev-parse", "HEAD"],
                                         capture_output=True, text=True).stdout.strip()})
    with ThreadPoolExecutor(1) as pool:
        f = pool.submit(run_probe, policy, trials, root, TASK)
        try:
            f.result()
        except Exception as e:  # a broken trial must not stop the rest
            log({"phase": "probe_error", "error": f"{type(e).__name__}: {e}"[:300]})


if __name__ == "__main__":
    main()
