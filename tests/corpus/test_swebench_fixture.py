"""Tests for the SWE-bench fixture seeder (corpus -> TRACE runner bridge).

The Django corpus lives in the external artifact cache and is exposed to the
runner through ``fixtures/swebench_repo``. These tests pin the seeder's contract
against a *tiny* generated repo, so they never touch the multi-hundred-MB real
clone and stay fast and deterministic.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.seed_swebench_repo import ensure_seeded_swebench  # noqa: E402

SEED_ENV = {
    "GIT_AUTHOR_NAME": "TRACE Test",
    "GIT_AUTHOR_EMAIL": "test@localhost",
    "GIT_COMMITTER_NAME": "TRACE Test",
    "GIT_COMMITTER_EMAIL": "test@localhost",
    "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00",
}


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(SEED_ENV)
    env["GIT_CONFIG_PARAMETERS"] = "'core.autocrlf=false'"
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _djangoish_fixture(tmp_path: Path, name: str) -> Path:
    """A minimal repo whose committed tree has Django's required entrypoints.

    Only the *committed tree* matters: the seeder probes ``HEAD:<path>`` because
    the real cache clone holds an object store with an unpopulated worktree.
    """
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "django").mkdir()
    (repo / "django" / "__init__.py").write_text(
        "__version__ = '4.1'\n", encoding="utf-8", newline=""
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "runtests.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8", newline=""
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add django-ish layout")
    return repo


def test_swebench_seeder_returns_head(tmp_path: Path):
    repo = _djangoish_fixture(tmp_path, "swebench_repo")
    head = ensure_seeded_swebench(repo)
    assert head == _git(repo, "rev-parse", "HEAD")


def test_swebench_seeder_resolves_object_store_without_worktree(tmp_path: Path):
    """The real cache has a complete object store but no checked-out files.

    The seeder must accept that shape: the runner clones from the fixture and
    checks out base_commit itself, so a populated working tree is not required.
    """
    repo = _djangoish_fixture(tmp_path, "swebench_repo")
    # Remove the working tree entirely, keeping .git intact.
    for entry in repo.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            import shutil

            shutil.rmtree(entry)
        else:
            entry.unlink()
    assert not (repo / "django").exists()
    # Still usable: the trees are in the object store.
    head = ensure_seeded_swebench(repo)
    assert head == _git(repo, "rev-parse", "HEAD")


def test_swebench_seeder_rejects_missing_entrypoints(tmp_path: Path):
    """A clone whose committed tree lacks the test entrypoint must be refused."""
    repo = tmp_path / "swebench_repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.rst").write_text("nothing here\n", encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "empty-ish")

    import pytest

    with pytest.raises(RuntimeError, match="incomplete"):
        ensure_seeded_swebench(repo)


def test_swebench_seeder_rejects_non_git_dir(tmp_path: Path):
    """A fixture path that is not a git clone must fail with a clear message."""
    plain = tmp_path / "swebench_repo"
    plain.mkdir()
    (plain / "runtests.py").write_text("x\n", encoding="utf-8", newline="")

    import pytest

    with pytest.raises(RuntimeError, match="not a usable git clone"):
        ensure_seeded_swebench(plain)


def test_swebench_seeder_rejects_partial_clone(tmp_path: Path):
    """A blobless (promisor) clone must be refused, never silently accepted.

    A partial clone would fetch blobs from the network during checkout, which is
    exactly the hidden network dependency this corpus must not have.
    """
    repo = _djangoish_fixture(tmp_path, "swebench_repo")
    _git(repo, "config", "remote.origin.partialclonefilter", "blob:none")

    import pytest

    with pytest.raises(RuntimeError, match="blobless"):
        ensure_seeded_swebench(repo)


def test_swebench_seeder_is_registered_in_workspace():
    """The runner resolves a seeder by fixture dir name; it must be registered.

    Without this the adapter's SWE-bench tasks cannot enter the existing runner
    at all ('no seeder registered for fixture').
    """
    from benchmark import workspace

    assert "swebench_repo" in workspace._SEEDERS
    assert workspace._SEEDERS["swebench_repo"] is ensure_seeded_swebench


def test_seeder_lookup_survives_path_resolution(tmp_path: Path):
    """A resolved fixture path must still find its seeder (junction case).

    On Windows `fixtures/swebench_repo` is an NTFS junction into the external
    cache clone, whose real directory name is `django__django`. `Path.resolve()`
    follows that link, so a name-keyed registry would otherwise miss and the
    runner would raise 'no seeder registered for fixture: django__django'.
    """
    from benchmark import workspace

    resolved = tmp_path / "django__django"
    resolved.mkdir()

    seeder, name = workspace._lookup_seeder(resolved)
    assert seeder is ensure_seeded_swebench, f"unexpected lookup name: {name}"


def test_seeder_lookup_prefers_declared_name_then_resolved(tmp_path: Path):
    """Declared name wins; resolved name is the fallback, not the primary."""
    from benchmark import workspace

    plain = tmp_path / "swebench_repo"
    plain.mkdir()
    seeder, name = workspace._lookup_seeder(plain)
    assert seeder is ensure_seeded_swebench
    assert name == "swebench_repo"


def test_seeder_lookup_still_fails_loudly_for_unknown_fixtures(tmp_path: Path):
    """Unknown fixtures must still raise, never silently seed the wrong repo."""
    import pytest

    from benchmark import workspace

    unknown = tmp_path / "not_a_registered_fixture"
    unknown.mkdir()
    seeder, name = workspace._lookup_seeder(unknown)
    assert seeder is None
    assert name == "not_a_registered_fixture"
    with pytest.raises(RuntimeError, match="no seeder registered"):
        workspace.ensure_fixture(unknown)


def test_swebench_seeder_verifies_requested_base_commit(tmp_path: Path):
    """A shared historical clone must be checked for the task's commit.

    Unlike generated fixtures, the SWE-bench clone has many commits and its
    HEAD is not the task's base_commit. The seeder is told which commit the
    task needs and must confirm that exact object exists locally, returning it
    so the runner's "fixture HEAD == base_commit" contract holds for the right
    reason.
    """
    repo = _djangoish_fixture(tmp_path, "swebench_repo")
    older = _git(repo, "rev-parse", "HEAD")

    # Add a later commit so HEAD moves past the task's base_commit.
    (repo / "tests" / "runtests.py").write_text(
        "# newer\n", encoding="utf-8", newline=""
    )
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@e.st", "-c", "user.name=t", "commit", "-m", "later")
    assert _git(repo, "rev-parse", "HEAD") != older

    # Asking for the historical commit validates it and reports it back.
    assert ensure_seeded_swebench(repo, base_commit=older) == older
    # Asking for the clone's own HEAD still works.
    assert ensure_seeded_swebench(repo) == _git(repo, "rev-parse", "HEAD")


def test_swebench_seeder_rejects_absent_base_commit(tmp_path: Path):
    """A base_commit the clone does not contain must fail loudly, not silently."""
    import pytest

    repo = _djangoish_fixture(tmp_path, "swebench_repo")
    bogus = "0" * 40
    with pytest.raises(RuntimeError, match="does not contain base_commit"):
        ensure_seeded_swebench(repo, base_commit=bogus)
