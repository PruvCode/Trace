"""One-shot: measure a full Django checkout on the shared scratch worktree.

Runs entirely against the external cache (never the Trace repo), cleans any
stale lock it owns, and prints a single machine-readable result line.

Usage: python scripts/_measure_checkout.py <commit>
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SCRATCH = Path(
    r"C:\Users\pruth\AppData\Local\TraceSWECache\scratch\django__django"
)
LOCK = Path(
    r"C:\Users\pruth\AppData\Local\TraceSWECache\repos\django__django"
    r"\.git\worktrees\django__django\index.lock"
)


def _git_env() -> dict:
    env = dict(os.environ)
    env["GIT_CONFIG_PARAMETERS"] = "'core.autocrlf=false' 'core.lockTimeout=30000'"
    return env


def _no_git_running() -> bool:
    out = subprocess.run(["tasklist"], capture_output=True, text=True).stdout
    return not [ln for ln in out.splitlines() if "git.exe" in ln.lower()]


def _clear_stale_lock() -> bool:
    if LOCK.exists() and _no_git_running():
        LOCK.unlink()
        return True
    return False


def _count_files() -> int:
    n = 0
    for root, dirs, files in os.walk(SCRATCH):
        if ".git" in dirs:
            dirs.remove(".git")
        n += len(files)
    return n


def main() -> int:
    commit = sys.argv[1] if len(sys.argv) > 1 else "f37face331f21cb8af70fc4ec101ec7b6be1f63e"
    cleared = _clear_stale_lock()
    print(f"stale_lock_cleared={cleared}", flush=True)
    print(f"commit={commit}", flush=True)
    print(f"files_before={_count_files()}", flush=True)
    print(f"midx_present={list((SCRATCH / '.git').glob('objects/pack/multi-pack-index*')) != [] or 'in shared repo'}", flush=True)

    start = time.time()
    proc = subprocess.run(
        ["git", "checkout", "--force", commit],
        cwd=str(SCRATCH),
        capture_output=True,
        text=True,
        env=_git_env(),
        timeout=3600,
    )
    elapsed = time.time() - start
    print(f"rc={proc.returncode}", flush=True)
    print(f"files_after={_count_files()}", flush=True)
    if (proc.stderr or "").strip():
        print(f"stderr={(proc.stderr or '').strip()[:500]}", flush=True)
    print(f"RESULT_ELAPSED={elapsed:.1f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
