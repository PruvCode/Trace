"""Proof that the materialized Django cache satisfies checkouts WITHOUT network.

Strategy: set GIT_ALLOW_PROTOCOL=file so git refuses https/http/git/ssh transports.
If the required commit/tree/blob objects resolve, they came from local object store,
not a promisor fetch.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

CLONE = Path(r"C:\Users\pruth\AppData\Local\TraceSWECache\repos\django__django")
TRACE = Path(r"C:\Users\pruth\OneDrive\Desktop\Trace")


def git(args, env):
    p = subprocess.run(["git", *args], cwd=str(CLONE), capture_output=True, text=True, env=env)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def main() -> int:
    probe = json.loads((TRACE / "benchmark" / "validation" / "_probe.json").read_text(encoding="utf-8"))
    commits: list[str] = []
    for row in probe["candidates"]:
        bc = row["base_commit"]
        if bc not in commits:
            commits.append(bc)
    commits = commits[:5]
    print(f"testing {len(commits)} base_commits (deduped, first 5) from the probe")

    env = dict(os.environ)
    env["GIT_ALLOW_PROTOCOL"] = "file"          # refuse any remote transport
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_PARAMETERS"] = "'core.autocrlf=false' 'core.lockTimeout=30000'"

    ok = 0
    for c in commits:
        t0 = time.time()
        rc_c, _, _ = git(["cat-file", "-e", c + "^{commit}"], env)
        rc_t, _, _ = git(["cat-file", "-e", c + "^{tree}"], env)
        rc_b, blob_sha, _ = git(["rev-parse", c + ":django/__init__.py"], env)
        has_blob = False
        if rc_b == 0 and blob_sha:
            rc_cb, _, _ = git(["cat-file", "-e", blob_sha], env)
            has_blob = rc_cb == 0
        dt = time.time() - t0
        alllocal = (rc_c == 0 and rc_t == 0 and has_blob)
        ok += 1 if alllocal else 0
        print(f"  {c[:12]}  commit={rc_c == 0}  tree={rc_t == 0}  src_blob={has_blob}  ({dt:.2f}s)")

    print(f"\nRESULT: {ok}/{len(commits)} commits fully resolvable with network transports disabled")
    return 0 if ok == len(commits) else 1


if __name__ == "__main__":
    raise SystemExit(main())
