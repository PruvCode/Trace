"""Deterministically seed fixtures/toy_repo as a git repo.

Tracked source files live in the main repo; the .git directory is generated
(gitignored) so workspaces can `git clone` a real repo at a pinned commit.

Determinism: fixed author/committer identity AND dates, plus
core.fileMode=false and core.autocrlf=false, so the resulting SHA is stable
across runs on the same file content. Prints the HEAD SHA (paste into the
task's task.yaml as base_commit).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "toy_repo"

SEED_ENV = {
    "GIT_AUTHOR_NAME": "TRACE Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@localhost",
    "GIT_COMMITTER_NAME": "TRACE Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@localhost",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}

BASE_GIT_ARGS = [
    "-c",
    "core.fileMode=false",
    "-c",
    "core.autocrlf=false",
]


def _git(*args: str, cwd: Path) -> str:
    import os

    env = dict(os.environ)
    env.update(SEED_ENV)
    proc = subprocess.run(
        ["git", *BASE_GIT_ARGS, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def ensure_seeded_fixture(fixture_dir: Path = FIXTURE_DIR) -> str:
    """Create/commit the fixture repo if needed; return HEAD SHA."""
    if not (fixture_dir / "auth" / "session.py").exists():
        raise FileNotFoundError(f"fixture source missing in {fixture_dir}")
    git_dir = fixture_dir / ".git"
    if not git_dir.exists():
        _git("init", "-b", "main", cwd=fixture_dir)
        _git("config", "core.fileMode", "false", cwd=fixture_dir)
        _git("config", "core.autocrlf", "false", cwd=fixture_dir)
        _git("add", "-A", cwd=fixture_dir)
        _git("commit", "-m", "seed: toy_repo v1", cwd=fixture_dir)
    head = _git("rev-parse", "HEAD", cwd=fixture_dir)
    status = _git("status", "--porcelain", cwd=fixture_dir)
    if status:
        raise RuntimeError(f"fixture repo has uncommitted changes:\n{status}")
    return head


def main() -> int:
    head = ensure_seeded_fixture()
    print(head)
    return 0


if __name__ == "__main__":
    sys.exit(main())
