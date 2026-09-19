"""Cache location resolution for large SWE-bench artifacts.

SWE-bench artifacts (the ~12 MB test-split parquet and a full django/django
clone) must NOT live inside the repository working tree: the repo is on a
OneDrive-synced Desktop, and a multi-hundred-MB git object store there would
both thrash sync and pollute the checkout.

Resolution order for the cache root:
1. ``$TRACE_SWE_CACHE`` if set,
2. ``%LOCALAPPDATA%/TraceSWECache`` on Windows,
3. ``~/.cache/trace-swe`` elsewhere.

The resolved root is recorded in every manifest so a corpus is traceable to
the exact on-disk data that produced it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DATASET_REPO_ID = "princeton-nlp/SWE-bench"
DATASET_REVISION = "e48e2bd1e9fecd5bbd641e9414ac59da9f2e69f6"
DATASET_SPLIT_FILE = "test.parquet"
DATASET_SPLIT_NAME = "test"
DATASET_PARQUET_SHA256 = (
    "db4f70ef735b3162c74801ddcdf8d7bae8d704193788c6d844f898c20b571cbb"
)
DATASET_URL = (
    "https://huggingface.co/datasets/princeton-nlp/SWE-bench/resolve/"
    f"{DATASET_REVISION}/data/test-00000-of-00001.parquet"
)


def cache_root() -> Path:
    """Resolve the external artifact cache root (never inside the repo)."""
    env = os.environ.get("TRACE_SWE_CACHE")
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return (Path(base) / "TraceSWECache").resolve()
    return (Path.home() / ".cache" / "trace-swe").resolve()


def dataset_dir() -> Path:
    return cache_root() / "datasets" / DATASET_REPO_ID.replace("/", "__")


def dataset_parquet_path() -> Path:
    return dataset_dir() / DATASET_SPLIT_FILE


def repos_dir() -> Path:
    """Parent dir for cached upstream repository clones (e.g. django/django)."""
    return cache_root() / "repos"


def repo_clone_dir(repo: str) -> Path:
    return repos_dir() / repo.replace("/", "__")


def scratch_repo_dir(repo: str) -> Path:
    """Single reusable worktree path per repo (guard-safe: one, not per-task).

    Validation materializes exactly one worktree and resets it in place, so a
    thousands-of-files checkout is never recursively deleted between
    candidates.

    Location matters enormously on Windows. A checkout of Django churns ~6,600
    files, and *deleting* files under ``%LOCALAPPDATA%`` is throttled to ~3
    files/s by a filesystem filter (OneDrive/Defender class), versus ~900
    files/s under ``%TEMP%``. The same directory tree that costs ~30 minutes to
    switch commits under AppData\\Local costs seconds under Temp, so the
    worktree is placed on the fast path while the object store (read-only
    during a checkout) stays in the cache.

    Override with ``TRACE_SWE_SCRATCH`` when a different volume is preferred.
    """
    env = os.environ.get("TRACE_SWE_SCRATCH")
    if env:
        return Path(env).expanduser().resolve() / repo.replace("/", "__")
    if sys.platform == "win32":
        # %TEMP% is not subject to the AppData delete-throttling observed here.
        base = os.environ.get("TEMP") or os.environ.get("TMP")
        if base and not base.startswith(str(cache_root())):
            return (Path(base) / "trace_scratch" / repo.replace("/", "__")).resolve()
    return cache_root() / "scratch" / repo.replace("/", "__")


def validation_dir(repo_root: Path) -> Path:
    """In-repo location for machine-readable validation artifacts."""
    return repo_root / "benchmark" / "validation"


def candidates_dir(repo_root: Path) -> Path:
    """In-repo candidate pool (underscore-prefixed: never a runnable task)."""
    return repo_root / "benchmark" / "tasks" / "_candidates"
