"""Local-only sanitizer for the five Phase 1 pilot pairs.

Reads the private review bundle and the OpenCode store READ-ONLY, and writes
minimized, content-free session timelines that may be shown to AI reviewers:

- tool family + status per tool call, in order
- shell commands reduced to allowlisted dev words; every other token becomes a
  per-run HMAC token (same value in both sessions of a pair, so overlap stays
  visible, but the value itself is never emitted)
- errors reduced to an error class (exception type name / fixed phrases)
- user/assistant text: length only, never content

A privacy lint rejects the whole output if any emitted string falls outside
the allowed vocabulary. Raw ids, paths and text never leave this process.
Also emits a content-blind capture audit: for each session, OpenCode part
count vs rows the project's TRACE store holds for that session.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path

TEMP = Path(os.environ["TEMP"])
MANIFEST = TEMP / "trace_s1b" / "review" / "review_manifest.json"
REAL_DB = Path(os.environ["USERPROFILE"]) / ".local" / "share" / "opencode" / "opencode.db"
OUT = Path(sys.argv[1])
KEY = secrets.token_bytes(32)  # per run, never stored

WORDS = set("""
git status diff log add commit push pull checkout branch merge rebase stash show fetch clone init reset restore
python python3 py pip install uninstall -m pytest unittest npm npx node yarn pnpm bun run test build start dev lint
ls dir cd cat type echo mkdir rm del cp mv copy move find grep rg head tail wc which where curl wget make cmake cargo go
uv venv activate deactivate pwsh powershell bash sh cmd set export env opencode trace docker compose up down
tsc eslint prettier black ruff mypy flake8 jest vitest playwright sqlite3 && || | > >> < ; 2>&1 -q -v -x -k -c -r -e -n
get-childitem select-object format-table get-content set-content select-string where-object foreach-object
test-path remove-item new-item copy-item move-item out-file get-process stop-process start-process measure-object
sort-object get-item write-output invoke-webrequest
""".split())
ERR_PHRASES = ["no such file", "not found", "permission denied", "timed out", "timeout", "syntax error",
               "command not found", "is not recognized", "connection refused", "already exists",
               "exit code", "failed", "cannot find", "access is denied", "not a git repository",
               "merge conflict", "assertion", "traceback", "aborted", "rejected", "unauthorized"]
ERR_TYPE = re.compile(r"\b([A-Z][A-Za-z]{2,40}(?:Error|Exception|Warning))\b")
EDIT = re.compile(r"^(edit|write|patch|multiedit|str_replace|create)", re.I)
SHELL = re.compile(r"^(bash|terminal|shell|zsh|powershell|cmd)", re.I)
READ = re.compile(r"^(read|grep|glob|list|search|webfetch|fetch)", re.I)
SAFE = re.compile(r"^(T[0-9a-f]{8}|[a-z_]+|-{1,2}[a-z][a-z-]{0,20}|[&|;<>]{1,2}|2>&1|\d{1,3}|"
                  r"[A-Z][A-Za-z]{2,40}(Error|Exception|Warning)|\.[a-z]{1,5})$")


def tok(raw: str) -> str:
    return "T" + hmac.new(KEY, raw.strip().lower().encode(), hashlib.sha256).hexdigest()[:8]


def family(tool: str) -> str:
    if not tool:
        return "unknown"
    if tool.startswith(("trace-memory", "trace_memory")):
        return "trace_memory"
    for name, pat in (("edit", EDIT), ("shell", SHELL), ("read", READ)):
        if pat.match(tool):
            return name
    return "other_tool"


def ext_of(path: str) -> str:
    m = re.search(r"(\.[A-Za-z0-9]{1,5})$", path)
    return m.group(1).lower() if m else ""


def shell_skeleton(cmd: str) -> list[str]:
    out = []
    for t in re.findall(r"2>&1|&&|\|\||[|;<>]+|\"[^\"]*\"|'[^']*'|\S+", cmd)[:24]:
        low = t.lower()
        if low in WORDS:
            out.append(low)
        elif re.fullmatch(r"-{1,2}[a-z][a-z-]{0,20}", low):
            out.append(low)
        else:
            out.append(tok(t) + ext_of(t.strip("\"'")))
    return out


def target_of(inp: dict) -> list[str]:
    keys = ("filePath", "file_path", "path", "pattern", "url", "query", "symbol", "include")
    return [tok(str(inp[k])) + ext_of(str(inp[k])) for k in keys if isinstance(inp.get(k), str) and inp[k]]


def err_class(text: str) -> list[str]:
    if not text:
        return []
    low = text.lower()
    cls = sorted(set(ERR_TYPE.findall(text)))[:3]
    cls += [p for p in ERR_PHRASES if p in low][:3]
    return cls


def lint(obj) -> list[str]:
    bad = []

    def walk(v, where):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, where + "." + k)
        elif isinstance(v, list):
            for x in v:
                walk(x, where)
        elif isinstance(v, str):
            if v in ERR_PHRASES or v in WORDS or SAFE.match(v):
                return
            if re.fullmatch(r"T[0-9a-f]{8}\.[a-z0-9]{1,5}", v):
                return
            bad.append(where)
    walk(obj, "$")
    return bad


def timeline(conn, sid: str) -> dict:
    steps, user_lens, asst_lens = [], [], []
    for (data,) in conn.execute("SELECT data FROM message WHERE session_id=? ORDER BY time_created", (sid,)):
        try:
            m = json.loads(data)
        except ValueError:
            continue
        (user_lens if m.get("role") == "user" else asst_lens).append(0)
    for (data,) in conn.execute("SELECT data FROM part WHERE session_id=? ORDER BY time_created, id", (sid,)):
        try:
            p = json.loads(data)
        except ValueError:
            continue
        if p.get("type") == "text" and isinstance(p.get("text"), str):
            continue
        if p.get("type") != "tool":
            continue
        st = p.get("state") if isinstance(p.get("state"), dict) else {}
        inp = st.get("input") if isinstance(st.get("input"), dict) else {}
        fam = family(p.get("tool") or "")
        step = {"fam": fam, "status": st.get("status") or "unknown"}
        if fam == "shell" and isinstance(inp.get("command"), str):
            step["cmd"] = shell_skeleton(inp["command"])
        else:
            tg = target_of(inp)
            if tg:
                step["target"] = tg
        out = st.get("error") if st.get("status") == "error" else None
        if out is None and fam == "shell" and isinstance(st.get("output"), str):
            # A shell call can "complete" while the command itself failed.
            out = st["output"][-2000:]
        ec = err_class(out or "")
        if ec:
            step["err"] = ec
        steps.append(step)
    return {"messages": len(user_lens) + len(asst_lens), "user_messages": len(user_lens),
            "tool_calls": len(steps), "steps": steps}


def capture_audit(conn, sid: str, directory: str) -> dict:
    parts = conn.execute("SELECT COUNT(*) FROM part WHERE session_id=?", (sid,)).fetchone()[0]
    store = Path(directory) / ".agent-memory" / "memory.db" if directory else None
    res = {"opencode_parts": parts, "trace_store": "absent", "captured_rows": None}
    if store and store.exists():
        try:
            t = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
            tables = {r[0] for r in t.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            res["trace_store"] = "present"
            res["captured_rows"] = (t.execute("SELECT COUNT(*) FROM transcript_messages WHERE session_id=?",
                                              (sid,)).fetchone()[0]
                                    if "transcript_messages" in tables else "no transcript table")
            t.close()
        except sqlite3.Error as e:
            res["trace_store"] = "unreadable:" + type(e).__name__
    return res


def main() -> None:
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True, timeout=30)
    pairs, audit = [], []
    for i, p in enumerate(man["pairs"], 1):
        sides = {}
        for side in ("earlier", "later"):
            sid = p[f"{side}_raw_sid"]
            row = conn.execute("SELECT directory FROM session WHERE id=?", (sid,)).fetchone()
            sides[side] = timeline(conn, sid)
            audit.append({"pair": f"pair{i}", "side": side, **capture_audit(conn, sid, row[0] if row else "")})
        pairs.append({"pair": f"pair{i}", "gap_bucket": ("<1h" if p["gap_s"] < 3600 else "1-24h" if p["gap_s"] < 86400
                                                       else "1-7d" if p["gap_s"] < 604800 else ">7d"),
                      **sides})
    conn.close()
    bad = lint([{s: p[s] for s in ("earlier", "later")} for p in pairs])
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {"pairs": len(pairs), "lint_violations": len(bad),
               "lint_paths": sorted(set(bad))[:20], "capture_audit": audit}
    (OUT / "sanitize_report.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    if bad:
        print(json.dumps(summary))
        raise SystemExit("privacy lint failed; excerpts NOT written")
    (OUT / "excerpts_sanitized.json").write_text(json.dumps({"pairs": pairs}, separators=(",", ":")), encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
