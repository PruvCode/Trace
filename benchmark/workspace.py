"""Isolated workspaces: every run starts from the same pinned commit.

Phase 1 strategy: `git clone <seeded fixture> <run_dir>` + checkout of
task.base_commit. Clone (not worktree) is the primary mechanism because the
fixture is a generated repo outside the main checkout; the choice stays hidden
behind create_workspace() so a worktree implementation can replace it later
without touching the runner. No shell; list-form git args only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.seed_deps_repo import ensure_seeded_deps  # noqa: E402
from scripts.seed_toy_repo import ensure_seeded_fixture  # noqa: E402

GIT_TIMEOUT = 120

# Fixture-name -> seeder. Each seeder takes the fixture dir and returns HEAD.
# Unknown fixtures fail loudly: silently seeding the wrong repo would corrupt
# benchmark provenance. (history_repo dispatch arrives with Phase 5.3 tasks.)
_SEEDERS = {
    "toy_repo": ensure_seeded_fixture,
    "deps_repo": ensure_seeded_deps,
}


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def ensure_fixture(fixture_dir: Path) -> str:
    """Seed the fixture repo if needed; return its HEAD SHA."""
    env_name = os.environ.get("TRACE_SEED_DATES", "")
    _ = env_name
    seeder = _SEEDERS.get(fixture_dir.name)
    if seeder is None:
        raise RuntimeError(
            f"no seeder registered for fixture: {fixture_dir.name}"
        )
    return seeder(fixture_dir)


def create_workspace(fixture_dir: Path, base_commit: str, dest: Path) -> Path:
    """Clone the fixture at base_commit into dest (fresh dir per run)."""
    if dest.exists():
        raise FileExistsError(f"workspace destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", str(fixture_dir.resolve()), str(dest)],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed: {proc.stderr.strip()}")
    _git("checkout", base_commit, cwd=dest)
    head = _git("rev-parse", "HEAD", cwd=dest)
    if head != base_commit:
        raise RuntimeError(f"workspace HEAD {head} != base_commit {base_commit}")
    return dest


def workspace_git_status(workspace: Path) -> list[str]:
    """Porcelain status lines (dirty-file snapshot for provenance)."""
    out = _git("status", "--porcelain", cwd=workspace)
    return out.splitlines() if out else []


def workspace_head(workspace: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=workspace)
