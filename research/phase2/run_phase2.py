"""Phase 2: controlled two-session comparison of memory conditions.

Per task: ONE shared Session A (real OpenCode run, TRACE capture on, asked to
append a handoff to AGENTS.md), then Session B with the task prompt under:

  C0  no memory          (AGENTS.md removed, no TRACE)
  C1  agent notes        (AGENTS.md from Session A kept, no TRACE)
  C2  TRACE recency      (TRACE store from A, plugin + MCP, TRACE_TASK_AWARE=0)
  C3  TRACE task-aware   (same, query = B's prompt, TRACE_TASK_AWARE=1)

B always starts from the task's base code (D tasks: A ran on an OLDER code
state, so anything A learned is stale). Fixture copies carry no git history
and no "# BUG" comments, so prior knowledge can only come from Session A.
Scoring: the task's hidden evaluator. Prints one JSON line per run.
"""

from __future__ import annotations

import io
import json
import os
import random
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
# GLM adoption fix (phase/2-eval): the prior agent hardcoded a OneDrive venv path;
# run from the current interpreter so the harness works from any checkout.
PY = sys.executable
OPENCODE = Path(os.environ["TEMP"]) / "trace_tools" / "oc118" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
MODEL = "opencode/big-pickle"
CONDITIONS = ["C0", "C1", "C2", "C3"]
HANDOFF = ("\n\nBefore you finish, append a short handoff for future sessions to AGENTS.md in the project root: "
           "what you learned, any decisions or constraints you were told, and anything that failed.")
LOCK = threading.Lock()

CHECK_BATCH = ('"""Batch renewal smoke check: service accounts renew with an empty user id."""\n'
               "from services.renewal import renew_session\n\n"
               'assert renew_session("") == "token--fresh", "batch renewal for service accounts broke"\n'
               'print("batch renewal OK")\n')

TASKS = {
    "C_episodic/task_01_failed_attempt": dict(
        extra={"check_batch.py": CHECK_BATCH}, facts=["login"],
        a=("Logins with an empty user ID should be rejected. Try the quick fix first: make renew_session in "
           "services/renewal.py raise ValueError for empty user IDs, then run `python check_batch.py`. If that "
           "breaks anything, revert your change and explain where the check should go instead. Do not implement "
           "the alternative.")),
    "C_episodic/task_02_decision": dict(
        facts=["v2:"],
        a=("Heads-up from the platform team about an upcoming change: token format v2. New tokens carry a `v2:` "
           "version prefix in front of the normal token, e.g. `v2:token-abc`. When we implement it, validate_token "
           "must accept both legacy `token-...` and `v2:token-...` tokens and reject everything else. For now just "
           "read auth/tokens.py and tell me where that change would go. Do not change any code.")),
    "C_episodic/task_03_incident_blocklist": dict(
        facts=["u-comp-7", "u-comp-19"],
        a=("Security incident on 2026-02-20: accounts u-comp-7 and u-comp-19 were compromised. Tokens issued for "
           "those accounts must stop verifying; issuance for everyone else stays unchanged. Today just read "
           "auth/tokens.py and explain how tokens are issued and validated. Do not change code; the fix happens "
           "in a follow-up session.")),
    "C_episodic/task_04_reverted_cache_key": dict(
        facts=["name"],
        a=("Someone proposed keying the user cache in cache/users.py by username to speed up lookups. With the "
           "current code, run a quick python one-liner that calls get_user('u1') and get_user('u2') (both users "
           "are named 'ann'), and report whether the results are correct and why. Do not change code.")),
    "C_episodic/task_05_cursor_rollout": dict(
        facts=["2026-09-01"],
        a=("Platform decision for the opaque-cursor rollout: decoders must keep accepting legacy numeric offset "
           "cursors (plain strings like '40') during the compatibility window until 2026-09-01; new cursors look "
           "like 'cur_40'. Read store/cursors.py and tell me where that would apply. Do not change code.")),
    "C_episodic/task_06_migration_idempotent": dict(
        facts=["bf-2026-03"],
        a=("Context for upcoming work: backfill bf-2026-03 completed on 2026-03-10 for the users table. Any "
           "remaining unprocessed rows still belong to bf-2026-03; we must not open a new migration id. Read "
           "migrate/backfill.py and summarize what it does. Do not change code.")),
    "D_staleness/task_01_suffix": dict(
        stale={"auth/tokens.py": ('return f"token-{user_id}-fresh"', 'return f"token-{user_id}"')},
        facts=["token-u1"],
        a="No code changes: what exact token format does refresh_token issue right now? Give the example for user u1."),
    "D_staleness/task_02_credential_rotation": dict(
        stale={"auth/login.py": ("cred_current_2026_b", "cred_legacy_2026_a")},
        facts=["cred_legacy_2026_a"],
        a="No code changes: which service credential does the login module currently define? Quote the exact value."),
    "D_staleness/task_03_stale_page_default": dict(
        stale={"conf.py": ("PAGE_SIZE = 50", "PAGE_SIZE = 10")},
        facts=["10"],
        a="No code changes: what page size is the service configured for? Give the exact number and where it is set."),
    "D_staleness/task_04_stale_route": dict(
        stale={"router.py": ("/v2/", "/v1/")},
        facts=["/v1/sessions"],
        a="No code changes: which path does the service register for sessions? Quote it exactly."),
}


def log(obj: dict) -> None:
    with LOCK:
        print(json.dumps(obj), flush=True)


def env_for(home: Path, extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PWD", "PYTHONPATH")}
    for name in ("data", "config", "cache", "state"):
        (home / name).mkdir(parents=True, exist_ok=True)
        env[f"XDG_{name.upper()}_HOME"] = str(home / name)
    env["PYTHONPATH"] = str(WT)  # TRACE under test = this worktree
    env.update(extra or {})
    return env


def make_workspace(task: dict, ws: Path, stale: bool) -> None:
    fixture = WT / task["fixture"]
    blob = subprocess.run(["git", "-C", str(fixture), "archive", task["base_commit"]],
                          capture_output=True, check=True).stdout
    ws.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extractall(ws, filter="data")
    for path in ws.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        path.write_text(re.sub(r"[ \t]*#\s*BUG\b.*$", "", text, flags=re.M), encoding="utf-8")
    for name, body in task["spec"].get("extra", {}).items():
        (ws / name).write_text(body, encoding="utf-8")
    if stale:
        for name, (old, new) in task["spec"].get("stale", {}).items():
            path = ws / name
            text = path.read_text(encoding="utf-8")
            assert old in text, (name, old)
            path.write_text(text.replace(old, new), encoding="utf-8")


def trace_py(code: str, ws: Path, env: dict, *args: str, timeout: int = 300) -> dict:
    p = subprocess.run([str(PY), "-c", code, str(ws), *args], cwd=ws, env=env,
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


def run_opencode(ws: Path, env: dict, prompt: str, timeout: int) -> dict:
    t = time.time()
    env = {**env, "PWD": str(ws)}
    try:
        p = subprocess.run([str(OPENCODE), "run", "--format", "json", "-m", MODEL, "--dir", str(ws), prompt],
                           cwd=ws, env=env, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        out, rc = p.stdout, p.returncode
    except subprocess.TimeoutExpired as e:
        out, rc = e.stdout or b"", "timeout"
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


def evaluate(task: dict, ws: Path) -> bool:
    cmd = [str(PY) if part == "python" else part for part in task["eval_command"]]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(ws)
    try:
        return subprocess.run(cmd, cwd=ws, env=env, capture_output=True, timeout=120).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def store_has(ws: Path, facts: list[str]) -> bool | None:
    db = ws / ".agent-memory" / "memory.db"
    if not db.exists():
        return None
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    text = "\n".join(r[0] or "" for r in c.execute("SELECT content FROM transcript_messages"))
    c.close()
    return all(f in text for f in facts)


def run_task(name: str, root: Path, reps: int, seed: int) -> None:
    spec = TASKS[name]
    raw = yaml.safe_load((WT / "tasks" / name / "task.yaml").read_text(encoding="utf-8"))
    task = {"fixture": raw["fixture"], "base_commit": raw["base_commit"],
            "eval_command": raw["eval_command"], "spec": spec}
    b_prompt = (WT / "tasks" / name / "prompt.md").read_text(encoding="utf-8").strip()
    tdir = root / name.replace("/", "__")
    a_home, a_ws = tdir / "A" / "home", tdir / "A" / "proj"
    make_workspace(task, a_ws, stale=True)
    a_env = env_for(a_home)
    subprocess.run([str(OPENCODE), "models"], env=a_env, capture_output=True, timeout=300)
    setup = trace_py(SETUP, a_ws, a_env)
    a = run_opencode(a_ws, a_env, spec["a"] + HANDOFF, timeout=900)
    flush = trace_py(STOP, a_ws, a_env, "capture")
    agents = a_ws / "AGENTS.md"
    notes = agents.read_text(encoding="utf-8") if agents.exists() else ""
    log({"task": name, "phase": "A", **a, "setup": setup, "flush": flush,
         "agents_md_chars": len(notes), "agents_md_has_facts": all(f in notes for f in spec["facts"]),
         "store_has_facts": store_has(a_ws, spec["facts"]), "a_passes_eval_on_stale_code": evaluate(task, a_ws)})
    rng = random.Random(f"{seed}:{name}")
    for rep in range(reps):
        order = CONDITIONS[:]
        rng.shuffle(order)
        for cond in order:
            bdir = tdir / f"B_{cond}_r{rep}"
            ws, home = bdir / "proj", bdir / "home"
            make_workspace(task, ws, stale=False)
            shutil.copytree(a_home, home)
            extra = {}
            if cond == "C1" and notes:
                (ws / "AGENTS.md").write_text(notes, encoding="utf-8")
            if cond in ("C2", "C3"):
                shutil.copytree(a_ws / ".agent-memory", ws / ".agent-memory")
                extra["TRACE_TASK_AWARE"] = "1" if cond == "C3" else "0"
            env = env_for(home, extra)
            bsetup = trace_py(SETUP, ws, env) if cond in ("C2", "C3") else None
            b = run_opencode(ws, env, b_prompt, timeout=900)
            if cond in ("C2", "C3"):
                trace_py(STOP, ws, env)
            rlog = ws / ".agent-memory" / "retrieval.log"
            rows = [json.loads(x) for x in rlog.read_text(encoding="utf-8").splitlines() if x.strip()] \
                if rlog.exists() else []
            rows = [r for r in rows if r.get("sessionID") == b["session"]]  # store copied from A
            log({"task": name, "phase": "B", "cond": cond, "rep": rep, **b, "pass": evaluate(task, ws),
                 "tokens": tokens(home, b["session"]), "setup": bsetup,
                 "injected_requests": sum(1 for r in rows if r.get("injected")), "requests": len(rows),
                 "injected_chars": max([r.get("chars", 0) for r in rows] or [0])})


def main() -> None:
    root = Path(sys.argv[1])
    reps = int(sys.argv[2])
    workers = int(sys.argv[3])
    names = sys.argv[4:] or list(TASKS)
    root.mkdir(parents=True, exist_ok=True)
    log({"phase": "start", "model": MODEL, "opencode": str(OPENCODE), "tasks": names, "reps": reps,
         "conditions": CONDITIONS, "worktree_head": subprocess.run(
             ["git", "-C", str(WT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()})
    with ThreadPoolExecutor(workers) as pool:
        for f in [pool.submit(run_task, n, root, reps, 20261028) for n in names]:
            try:
                f.result()
            except Exception as e:  # one broken task must not stop the others
                log({"phase": "task_error", "error": f"{type(e).__name__}: {e}"[:300]})


if __name__ == "__main__":
    main()
