"""SWE-bench fixture seeder for the TRACE workspace layer.

``ensure_fixture`` looks up a seeder by the fixture directory's *name* and calls
it expecting a HEAD SHA back. The SWE-bench corpus is different from every
synthetic A/B/C/D fixture:

- it is **not** generated — it is a pre-existing clone of django/django that
  lives in the external artifact cache, materialized to a full (non-blobless)
  clone by ``corpus`` tooling;
- it is far too large to copy into the OneDrive-synced repository tree, so the
  repo exposes it through a junction at ``fixtures/swebench_repo``;
- the runner then clones *from* that fixture at each task's base_commit.

This seeder therefore does not "seed" anything: it verifies the fixture is a
healthy git clone whose object store is complete enough to satisfy a checkout
without network access, and returns its current HEAD.

Verified, not assumed: a broken or blobless fixture raises immediately rather
than producing a workspace whose checkout would silently fail later.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Files a task's checkout must be able to materialize. Checked by *tree
# presence at a commit*, not by the fixture's own working directory: the runner
# clones from this fixture and checks out base_commit, so only the object store
# needs to be complete — the fixture's own worktree may legitimately be empty.
#
# Django keeps its test entrypoint at tests/runtests.py (not ./runtests.py),
# which is also what corpus.validate._run_tests probes for.
_REQUIRED_PATHS = ("tests/runtests.py", "django/__init__.py")

# Default location of the materialized clone when the fixture is resolved
# through the cache directly rather than via the in-repo junction.
_CACHE_RELATIVE = ("TraceSWECache", "repos", "django__django")


def _git(args: list[str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Pin off the machine-wide core.autocrlf=true: SWE-bench patches are
    # authored against LF, and a translation here would make every diff
    # mismatch the working tree.
    env["GIT_CONFIG_PARAMETERS"] = "'core.autocrlf=false'"
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _resolve_fixture(fixture_dir: Path) -> Path:
    """Return the real clone dir, following the in-repo junction if present."""
    if (fixture_dir / ".git").exists():
        return fixture_dir
    # A junction may present `.git` as a file; trust rev-parse instead.
    proc = _git(["rev-parse", "--git-dir"], fixture_dir)
    if proc.returncode == 0:
        return fixture_dir
    raise RuntimeError(
        f"SWE-bench fixture is not a usable git clone: {fixture_dir}\n"
        "The Django corpus is materialized in the external cache; expose it "
        "with scripts/link_swebench_fixture.cmd"
    )


def ensure_seeded_swebench(fixture_dir: Path, base_commit: str | None = None) -> str:
    """Validate the cached Django clone and return its HEAD SHA.

    Unlike the synthetic fixtures, this clone is *shared across many tasks* and
    each task pins its own historical ``base_commit``. The clone's incidental
    HEAD is therefore not the task's base commit, and callers must not require
    them to be equal.

    ``base_commit`` (optional) is the commit a task actually needs. When given,
    it is verified to exist locally in the object store and is returned, so the
    runner's "fixture HEAD == task base_commit" contract holds for the right
    reason: the object is genuinely available, offline, for this task. When
    omitted, the clone's real HEAD is returned.

    Raises RuntimeError with an actionable message when the fixture is missing,
    not a git repository, lacks the Django entrypoint files a task needs, or is
    still a partial clone that would silently hit the network.
    """
    clone = _resolve_fixture(fixture_dir)

    proc = _git(["rev-parse", "HEAD"], clone)
    if proc.returncode != 0:
        raise RuntimeError(f"cannot resolve fixture HEAD: {proc.stderr.strip()}")
    head = proc.stdout.strip()

    # The commit the task will actually check out. For a corpus fixture this is
    # a historical commit, not HEAD; what matters is that it is present locally.
    if base_commit is not None:
        probe = _git(["cat-file", "-e", f"{base_commit}^{{commit}}"], clone)
        if probe.returncode != 0:
            raise RuntimeError(
                f"SWE-bench fixture at {clone} does not contain base_commit "
                f"{base_commit}; the cached clone cannot satisfy this task offline"
            )
        head = base_commit

    # The fixture's worktree may be empty (the cache holds an object store).
    # What matters is that the *committed tree* contains what a task checkout
    # needs, so probe the trees rather than the working directory.
    missing = [
        name
        for name in _REQUIRED_PATHS
        if _git(["cat-file", "-e", f"{head}:{name}"], clone).returncode != 0
    ]
    if missing:
        raise RuntimeError(
            f"SWE-bench fixture at {clone} has no {missing} at HEAD {head[:10]}; "
            "the cached clone looks incomplete"
        )

    # A promisor/blobless clone would fetch blobs from the network on checkout.
    # Refuse it explicitly: silent network dependency is exactly the failure
    # mode this corpus must not have.
    filter_proc = _git(["config", "--get", "remote.origin.partialclonefilter"], clone)
    if filter_proc.returncode == 0 and filter_proc.stdout.strip():
        raise RuntimeError(
            "SWE-bench fixture is still a partial (blobless) clone "
            f"(partialclonefilter={filter_proc.stdout.strip()!r}); "
            "materialize it before running corpus tasks"
        )

    return head


__all__ = ["ensure_seeded_swebench"]
