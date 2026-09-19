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

from scripts.seed_api_repo import ensure_seeded_api  # noqa: E402
from scripts.seed_cache_repo import ensure_seeded_cache  # noqa: E402
from scripts.seed_creds_repo import ensure_seeded_creds  # noqa: E402
from scripts.seed_data_repo import ensure_seeded_data  # noqa: E402
from scripts.seed_deps_repo import ensure_seeded_deps  # noqa: E402
from scripts.seed_history_repo import ensure_seeded_history  # noqa: E402
from scripts.seed_iface_repo import ensure_seeded_iface  # noqa: E402
from scripts.seed_net_repo import ensure_seeded_net  # noqa: E402
from scripts.seed_pager_repo import ensure_seeded_pager  # noqa: E402
from scripts.seed_profiles_repo import ensure_seeded_profiles  # noqa: E402
from scripts.seed_queue_repo import ensure_seeded_queue  # noqa: E402
from scripts.seed_store_repo import ensure_seeded_store  # noqa: E402
from scripts.seed_swebench_repo import ensure_seeded_swebench  # noqa: E402
from scripts.seed_toy_repo import ensure_seeded_fixture  # noqa: E402

GIT_TIMEOUT = 120
# `git clone` of the materialized SWE-bench fixture copies a ~350 MB object
# store; with on-access antivirus scanning that costs minutes, not seconds.
# Metadata calls stay on GIT_TIMEOUT; only tree-materializing operations (clone
# and the initial checkout) use the larger budget. Mirrors
# corpus.validate.CHECKOUT_TIMEOUT so both layers agree on what "too slow"
# means for this repo.
CLONE_TIMEOUT = 3600
CHECKOUT_TIMEOUT = 3600

# Fixture-name -> seeder. Each seeder takes the fixture dir and returns HEAD.
# Unknown fixtures fail loudly: silently seeding the wrong repo would corrupt
# benchmark provenance. (ensure_seeded_history returns oldest-first SHAs, so
# history_repo takes the last element.)
_SEEDERS = {
    "toy_repo": ensure_seeded_fixture,
    "deps_repo": ensure_seeded_deps,
    "iface_repo": ensure_seeded_iface,
    "creds_repo": ensure_seeded_creds,
    "pager_repo": ensure_seeded_pager,
    "net_repo": ensure_seeded_net,
    "data_repo": ensure_seeded_data,
    "api_repo": ensure_seeded_api,
    "queue_repo": ensure_seeded_queue,
    "profiles_repo": ensure_seeded_profiles,
    "cache_repo": ensure_seeded_cache,
    "store_repo": ensure_seeded_store,
    "history_repo": lambda d: ensure_seeded_history(d)[-1],
    # SWE-bench corpus fixture: a pre-existing materialized django clone exposed
    # through fixtures/swebench_repo (a junction to the external cache). It is
    # verified, never generated.
    "swebench_repo": ensure_seeded_swebench,
    # Alias for the junction target: some callers (e.g. benchmark.runner) pass
    # an already-resolved path, whose directory name is the cache clone's real
    # name rather than the declared fixture name.
    "django__django": ensure_seeded_swebench,
}


def _git(*args: str, cwd: Path, timeout: int = GIT_TIMEOUT) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _lookup_seeder(fixture_dir: Path):
    """Resolve a seeder for a fixture path.

    The registry is keyed on the *declared* fixture name (the last path
    component as written in task.yaml), not the resolved target. On Windows a
    fixture may be an NTFS junction: `Path.resolve()` follows it, so
    `fixtures/swebench_repo` -> `...\\TraceSWECache\\repos\\django__django`
    would otherwise resolve to the name `django__django` and miss the
    registry entirely. Both names are accepted so a real directory that
    happens to be named after the repo still works.
    """
    for candidate in (fixture_dir.name, fixture_dir.resolve().name):
        seeder = _SEEDERS.get(candidate)
        if seeder is not None:
            return seeder, fixture_dir.name
    return None, fixture_dir.name


def ensure_fixture(fixture_dir: Path, base_commit: str | None = None) -> str:
    """Seed the fixture repo if needed; return the HEAD the task will use.

    ``base_commit`` is passed through to seeders that can accept it. Generated
    fixtures ignore it (their single commit *is* the base commit); the shared
    SWE-bench clone uses it to verify the requested historical commit exists
    locally. Seeders that do not accept the kwarg are called without it.
    """
    env_name = os.environ.get("TRACE_SEED_DATES", "")
    _ = env_name
    seeder, lookup_name = _lookup_seeder(fixture_dir)
    if seeder is None:
        raise RuntimeError(
            f"no seeder registered for fixture: {lookup_name}"
        )
    if base_commit is None:
        return seeder(fixture_dir)
    try:
        return seeder(fixture_dir, base_commit=base_commit)
    except TypeError:
        # Seeder takes only the fixture dir: fall back to its own contract.
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
        timeout=CLONE_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed: {proc.stderr.strip()}")
    _git("checkout", base_commit, cwd=dest, timeout=CHECKOUT_TIMEOUT)
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
