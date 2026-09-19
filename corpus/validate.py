"""Candidate validation: never trust dataset metadata blindly.

For each candidate we attempt, in order:

1. repository checkout works (cached clone contains base_commit),
2. base commit exists and is checkable out,
3. the task is reproducible (test spec present and parseable),
4. relevant tests can be identified (FAIL_TO_PASS labels non-empty),
5. the gold patch applies cleanly,
6. relevant tests PASS after applying the gold patch,
7. the task has an objective evaluator (the test spec itself),
8. the task is not obviously broken or ambiguous.

Validation results are cached in ``benchmark/validation/`` keyed by
instance id, so a re-run never re-clones or re-executes an unchanged
candidate. Every result carries an explicit ``failure_reason``.

Reality note (documented honestly, not hidden): SWE-bench tasks are authored
for Linux container environments. This validator runs on the host OS; when the
host cannot provide a runnable environment it records
``environment_unsupported`` rather than pretending the task passed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from corpus import cache, swebench_source

VALIDATION_VERSION = "2"
# Timeout for cheap, metadata-only git calls (cat-file, rev-parse, ls-files).
GIT_TIMEOUT = 600
# Timeout for calls that materialize a whole tree on disk. A Django checkout is
# ~6,600 files; with an on-access virus scanner in the write path this measures
# ~9-10 files/s on Windows/NTFS, i.e. ~11 minutes for one full checkout. The
# previous 600 s budget was *below* the real cost, so checkouts were killed
# mid-write and every candidate after the first was misreported as
# ``checkout_failed``. This budget is deliberately generous: it exists to catch
# a genuine hang, not to race a slow-but-healthy filesystem.
CHECKOUT_TIMEOUT = 3600
# Timeout for a full test-suite invocation over FAIL_TO_PASS labels.
TEST_TIMEOUT = 3600
# Git's own lock-contention window in milliseconds. Concurrent git invocations
# against the *same* index fail immediately unless this is set; the validator
# therefore exports it for every git call it makes.
GIT_LOCK_WAIT_MS = "30000"
# How long an index.lock must be untouched before we treat it as abandoned on
# age alone. Only used when we cannot enumerate processes; the authoritative
# test is ``_no_git_process_running()``.
STALE_LOCK_SECONDS = 600
# Minimum age before a lock is eligible for clearing. A lock younger than this
# may belong to a git we just spawned, and deleting it would corrupt the index.
MIN_LOCK_AGE_SECONDS = 3


def _no_git_process_running() -> bool:
    """True when no ``git`` process exists on this host.

    An ``index.lock`` with no git process behind it is definitively orphaned —
    left by a crashed or killed run — so it is safe to clear regardless of age.
    Windows has no ``/proc``, so tasklist is used; any failure to enumerate is
    treated as "a git may be running" (the conservative answer).
    """
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq git.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:  # noqa: BLE001 - conservative fallback
        return False
    out = (proc.stdout or "").lower()
    return "git.exe" not in out


def _git_env(env: dict | None = None) -> dict:
    """Environment for git calls: wait for a contended index instead of dying.

    Two host artefacts are neutralised here:

    - ``index.lock: File exists`` is a contention symptom, not a task property.
      ``core.lockTimeout`` makes git wait for a contended lock instead of
      aborting instantly, removing the whole class of spurious
      ``checkout_failed`` results under concurrent runs.
    - On Windows, ``core.autocrlf`` defaults to a translation that rewrites
      checked-out line endings. SWE-bench patches are authored against LF, so
      any translation would make every diff mismatch the working tree and get
      misreported as ``gold_patch_does_not_apply``. We pin it off.
    """
    merged = dict(env if env is not None else os.environ)
    merged["GIT_CONFIG_PARAMETERS"] = (
        f"'core.lockTimeout={GIT_LOCK_WAIT_MS}' 'core.autocrlf=false'"
    )
    # ``GIT_CONFIG_PARAMETERS`` wins over system/global/local config, but it is
    # a single-process channel: any *child* git that does not inherit this env
    # (or a bare ``git`` run by a human) still picks up the machine-wide
    # ``core.autocrlf=true``. ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n``/
    # ``GIT_CONFIG_VALUE_n`` is the modern, more robust equivalent and is
    # exported too so the pin survives shells that drop GIT_CONFIG_PARAMETERS.
    merged["GIT_CONFIG_COUNT"] = "2"
    merged["GIT_CONFIG_KEY_0"] = "core.autocrlf"
    merged["GIT_CONFIG_VALUE_0"] = "false"
    merged["GIT_CONFIG_KEY_1"] = "core.lockTimeout"
    merged["GIT_CONFIG_VALUE_1"] = GIT_LOCK_WAIT_MS
    return merged


def _run(
    cmd: list[str],
    cwd: Path,
    timeout: int = GIT_TIMEOUT,
    env=None,
    input_text: str | None = None,
):
    """Run a subprocess, optionally feeding ``input_text`` on stdin.

    ``input_text`` is required for ``git apply -``; without it git reads EOF and
    reports "No valid patches in input", which would masquerade as a patch
    failure on every candidate.

    The stdin payload is encoded and sent as *bytes* deliberately. With
    ``text=True`` Python would translate ``\\n`` to ``\\r\\n`` on stdin under
    Windows, and git would then reject every LF-terminated diff with
    "patch does not apply" — a pure host artefact that would make the whole
    corpus look invalid.
    """
    if input_text is None:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        timeout=timeout,
        env=env,
        input=input_text.encode("utf-8"),
    )
    # Decode for callers that inspect stdout/stderr as text.
    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


def _git(
    *args: str, cwd: Path, timeout: int = GIT_TIMEOUT
) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd, timeout=timeout, env=_git_env())


def _clear_stale_lock(path: Path) -> bool:
    """Remove an orphaned ``index.lock``; never one a live git could own.

    Two independent signals qualify a lock as orphaned:

    - **no git process is running at all** — the lock cannot be held, so it is
      a leftover from a crashed or killed run (authoritative); or
    - the lock has not been touched for ``STALE_LOCK_SECONDS`` (age fallback).

    A lock younger than ``MIN_LOCK_AGE_SECONDS`` is never removed even when no
    git is visible, because a git we just spawned may not have appeared in the
    process table yet. Returns True if a lock was cleared.
    """
    if not path.exists():
        return False
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    if age < MIN_LOCK_AGE_SECONDS:
        return False
    if age < STALE_LOCK_SECONDS and not _no_git_process_running():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _git_with_lock_retry(
    *args: str,
    cwd: Path,
    lock_path: Path | None = None,
    attempts: int = 3,
    timeout: int = GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run git, clearing orphaned locks and retrying transient contention.

    Distinguishes infrastructure noise from genuine failure: a lock error is
    retried (up to ``attempts``), clearing the lock only when it is provably
    orphaned (no git process alive, or aged past the fallback). Any other error
    is returned immediately.
    """
    last: subprocess.CompletedProcess | None = None
    for attempt in range(attempts):
        proc = _git(*args, cwd=cwd, timeout=timeout)
        if proc.returncode == 0:
            return proc
        last = proc
        stderr = proc.stderr or ""
        if lock_path is not None and "index.lock" in stderr:
            _clear_stale_lock(lock_path)
            # Give the owner a moment to finish before the next attempt.
            time.sleep(1.0 * (attempt + 1))
            continue
        return proc
    return last  # type: ignore[return-value]


def ensure_repo_clone(repo: str, *, allow_network: bool = True) -> Path:
    """Ensure a cached clone of ``repo`` exists; return its path.

    A plain (non-shallow) clone is required because SWE-bench base commits are
    spread across the full history. The clone lives in the external cache, not
    in the repository tree.
    """
    dest = cache.repo_clone_dir(repo)
    if (dest / ".git").exists():
        return dest
    if not allow_network:
        raise RuntimeError(f"repo clone missing and network disabled: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    proc = _run(["git", "clone", "--filter=blob:none", url, str(dest)], cwd=dest.parent)
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed for {repo}: {proc.stderr.strip()[-800:]}")
    return dest


def _worktree_lock_path(clone: Path, scratch: Path) -> Path:
    """Path of the worktree-specific ``index.lock`` git would contend for."""
    return clone / ".git" / "worktrees" / scratch.name / "index.lock"


def _index_is_intact(scratch: Path) -> bool:
    """True when the worktree index agrees with its HEAD commit.

    An interrupted git (a lock held while a run was killed) can leave the index
    empty: ``git ls-files`` returns nothing and every tracked path shows as
    staged-deleted. In that state ``git apply`` fails with "does not exist in
    index", which looks exactly like an unappliable gold patch.
    """
    proc = _git("ls-files", cwd=scratch)
    if proc.returncode != 0:
        return False
    return bool(proc.stdout.strip())


def _repair_index(scratch: Path, lock: Path) -> None:
    """Rebuild a corrupted worktree index from HEAD after clearing its lock."""
    _clear_stale_lock(lock)
    # read-tree repopulates the index from the commit without touching worktree
    # files, so this is cheap for a 6,500-file checkout.
    _git("read-tree", "HEAD", cwd=scratch)


def _reset_worktree_in_place(clone: Path, scratch: Path) -> None:
    """Reset an existing worktree to a clean HEAD without mass deletion.

    The previous strategy was ``git worktree remove --force`` followed by
    ``shutil.rmtree``. A Django checkout is thousands of files, so that pairs
    an enormous recursive delete with every candidate and trips the
    bulk-delete safety guard. Instead we clear tracked content *through git*
    (``reset --hard``) and remove only untracked files git itself lists
    (``clean -fd``) — no Python-side recursive deletion, no worktree removal.

    If a previous run was killed while holding the index lock, the index is
    left empty and every later patch apply fails spuriously; that is detected
    and repaired here before the reset.
    """
    lock = _worktree_lock_path(clone, scratch)
    if not _index_is_intact(scratch):
        _repair_index(scratch, lock)
    _git_with_lock_retry("reset", "--hard", cwd=scratch, lock_path=lock)
    # -d removes untracked dirs, -x would also nuke ignored files (build
    # artefacts); we keep it conservative and never touch ignored data.
    _git_with_lock_retry("clean", "-fd", cwd=scratch, lock_path=lock)
    if not _index_is_intact(scratch):
        # Second attempt: the lock was newer than STALE_LOCK_SECONDS and got
        # cleared on the retry path inside _git_with_lock_retry.
        _repair_index(scratch, lock)
        _git_with_lock_retry("reset", "--hard", cwd=scratch, lock_path=lock)


def _worktree_is_registered(clone: Path, scratch: Path) -> bool:
    """True when ``scratch`` is a live, registered worktree of ``clone``.

    ``git worktree prune`` (or a deleted ``.git/worktrees/<name>`` directory)
    leaves the scratch directory on disk with a dangling ``.git`` file. That
    directory is *not* usable as a worktree, so it must not be treated as one.
    """
    marker = scratch / ".git"
    if not marker.exists():
        return False
    proc = _git("rev-parse", "--git-dir", cwd=scratch)
    return proc.returncode == 0


def _scratch_worktree(repo: str, base_commit: str, scratch: Path) -> Path:
    """Materialize a DETACHED worktree at base_commit, reusing one workspace.

    Guard-safe by construction:
    - exactly one worktree path per repo, materialized once and reused;
    - re-materialization resets in place (see ``_reset_worktree_in_place``)
      rather than deleting the tree;
    - the canonical cached clone is never modified (worktrees share its object
      store read-only; ``git worktree add`` only writes metadata into it);
    - index contention is absorbed by ``_git_with_lock_retry``, so a stale lock
      from an interrupted run is never reported as a candidate failure;
    - a dangling scratch directory (left behind by ``git worktree prune``) is
      relocated out of the way and re-registered, never used half-broken.
    """
    clone = ensure_repo_clone(repo)
    scratch.parent.mkdir(parents=True, exist_ok=True)
    lock = _worktree_lock_path(clone, scratch)

    if (scratch / ".git").exists() and not _worktree_is_registered(clone, scratch):
        # Dangling worktree metadata: move it aside (a single rename, not a
        # recursive delete) so `worktree add` can succeed cleanly.
        stale = scratch.with_name(f"{scratch.name}.dangling")
        if stale.exists():
            stale = scratch.with_name(f"{scratch.name}.dangling.{int(time.time())}")
        try:
            scratch.rename(stale)
        except OSError:
            pass
        _git("worktree", "prune", cwd=clone)

    if _worktree_is_registered(clone, scratch):
        # Reuse: detach cleanly, then check out the new commit.
        _reset_worktree_in_place(clone, scratch)
        proc = _git_with_lock_retry(
            "checkout",
            "--detach",
            base_commit,
            cwd=scratch,
            lock_path=lock,
            timeout=CHECKOUT_TIMEOUT,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"worktree checkout failed for {base_commit[:10]}: "
                f"{(proc.stderr or '').strip()[-800:]}"
            )
        return scratch

    # Register a fresh worktree (no prior tree to delete). This is the step
    # that writes ~6,600 files, so it gets the checkout budget.
    proc = _git_with_lock_retry(
        "worktree", "add", "--detach", str(scratch), base_commit,
        cwd=clone, lock_path=lock, timeout=CHECKOUT_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"worktree add failed for {base_commit[:10]}: "
            f"{(proc.stderr or '').strip()[-800:]}"
        )
    return scratch


def _base_commit_exists(
    repo: str, base_commit: str, *, allow_network: bool = True
) -> bool:
    clone = ensure_repo_clone(repo, allow_network=allow_network)
    proc = _git("cat-file", "-e", f"{base_commit}^{{commit}}", cwd=clone)
    return proc.returncode == 0


def _apply_patch(
    tree: Path,
    patch_text: str,
    *,
    reverse: bool = False,
    lock_path: Path | None = None,
) -> bool:
    """Apply (or reverse) a patch; True on success.

    Thin boolean wrapper kept for callers that only need the verdict. New code
    that needs to *explain* a failure should use ``_apply_patch_detail``: the
    previous boolean-only contract discarded git's stderr entirely, which is why
    a hung/killed checkout surfaced as an empty-string failure reason.
    """
    return _apply_patch_detail(
        tree, patch_text, reverse=reverse, lock_path=lock_path
    )["applied"]


def _apply_patch_detail(
    tree: Path,
    patch_text: str,
    *,
    reverse: bool = False,
    lock_path: Path | None = None,
) -> dict:
    """Apply a patch and return ``{applied, stderr_tail, attempts}``.

    A missing/empty body is reported as ``applied=True`` with
    ``empty_body=True`` so the *caller* can decide whether that is acceptable.
    For ``test_patch`` an empty body is NOT success (see ``validate_candidate``),
    but ``_apply_patch`` itself must stay a pure mechanic.
    """
    if not patch_text.strip():
        return {"applied": True, "empty_body": True, "stderr_tail": "", "attempts": 0}

    args = ["apply", "--whitespace=nowarn"]
    if reverse:
        args.append("--reverse")

    attempts = 0
    proc = _run(["git", *args, "-"], cwd=tree, env=_git_env(), input_text=patch_text)
    attempts += 1
    if proc.returncode != 0:
        # Fall back to 3-way apply, which tolerates a context that has drifted
        # slightly from the diff's recorded hunk context.
        args3 = ["apply", "--3way", "--whitespace=nowarn"]
        if reverse:
            args3.append("--reverse")
        three = _run(
            ["git", *args3, "-"], cwd=tree, env=_git_env(), input_text=patch_text
        )
        attempts += 1
        if three.returncode == 0:
            return {
                "applied": True,
                "empty_body": False,
                "stderr_tail": "",
                "attempts": attempts,
            }
        proc = three
    if proc.returncode != 0 and proc.stderr and "index.lock" in proc.stderr:
        # Contention, not a patch problem: clear an abandoned lock and retry.
        if lock_path is not None:
            _clear_stale_lock(lock_path)
        time.sleep(1.0)
        proc = _run(
            ["git", *args, "-"], cwd=tree, env=_git_env(), input_text=patch_text
        )
        attempts += 1
    return {
        "applied": proc.returncode == 0,
        "empty_body": False,
        "stderr_tail": (proc.stderr or "").strip()[-600:],
        "attempts": attempts,
    }


def _python_for_version(version: str | None) -> str | None:
    """Host interpreter usable for a Django version, or None if unsupported.

    Django 4.1+ supports Python 3.11; older lines do not. Returning None lets
    the caller record an honest ``environment_unsupported`` instead of a bogus
    test failure.
    """
    if not version:
        return None
    try:
        major, minor = (int(x) for x in version.split(".")[:2])
    except ValueError:
        return None
    if (major, minor) >= (4, 1):
        return sys.executable
    return None


def validate_candidate(
    candidate: dict,
    *,
    run_tests: bool = True,
    allow_network: bool = True,
) -> dict:
    """Validate one candidate; return a machine-readable result dict.

    Follows SWE-bench's own evaluation semantics, which use *two* patches:

    ``patch``
        the gold source fix (what the agent is supposed to discover);
    ``test_patch``
        the test-side change that introduces/updates the FAIL_TO_PASS tests.
        Without it the new assertions do not exist, so post-patch tests can
        never pass and every candidate would be misreported as broken.

    Both are applied through the same byte-exact LF-safe path (binary stdin,
    ``core.autocrlf=false``), and every failure mode gets its own reason so an
    infrastructure problem can never be recorded as a bad candidate.
    """
    instance_id = candidate["instance_id"]
    repo = candidate["repo"]
    base_commit = candidate["base_commit"]
    result = {
        "task_id": instance_id,
        "source_task_id": instance_id,
        "repo": repo,
        "base_commit": base_commit,
        "version": candidate.get("version"),
        "validation_version": VALIDATION_VERSION,
        "valid": False,
        "base_checkout": False,
        "base_commit_exists": False,
        "tests_available": False,
        "gold_patch_applies": False,
        "test_patch_applies": False,
        "has_test_patch": False,
        "tests_fail_before_gold_patch": None,
        "tests_pass_after_gold_patch": None,
        "f2p_labels": None,
        "p2p_labels": None,
        "duration_seconds": None,
        "classification": None,
        "failure_reason": None,
    }
    started = time.time()

    test_spec = candidate.get("test_spec") or {}
    f2p_raw = list(test_spec.get("FAIL_TO_PASS") or [])
    p2p_raw = list(test_spec.get("PASS_TO_PASS") or [])
    result["p2p_labels"] = normalize_test_labels(p2p_raw)
    if not f2p_raw:
        result["failure_reason"] = "no_fail_to_pass_tests"
        return _finalize(result, "CANDIDATE_INVALID", started)
    result["tests_available"] = True

    # Environment gate before any (networked) repository access, so the reason
    # is accurate: an unsupported Django version is an environment problem, not
    # a task problem, and must be reported as such even if the labels are odd.
    interpreter = _python_for_version(candidate.get("version"))
    if run_tests and interpreter is None:
        result["failure_reason"] = (
            f"environment_unsupported: Django {candidate.get('version')} is not "
            f"runnable on host Python "
            f"{sys.version_info[0]}.{sys.version_info[1]}"
        )
        return _finalize(result, "DEPENDENCY_FAILURE", started)

    # Labels in a shape Django's runner can actually execute. Recorded so a
    # label-shape problem is visible instead of looking like a test failure.
    f2p = normalize_test_labels(f2p_raw)
    if not f2p:
        result["failure_reason"] = "no_runnable_test_labels"
        return _finalize(result, "CANDIDATE_INVALID", started)
    result["f2p_labels"] = f2p

    try:
        if not _base_commit_exists(repo, base_commit, allow_network=allow_network):
            result["failure_reason"] = "base_commit_missing_from_repo"
            return _finalize(result, "CANDIDATE_INVALID", started)
        result["base_commit_exists"] = True
    except Exception as exc:  # noqa: BLE001 - recorded, never raised
        result["failure_reason"] = f"repo_checkout_error: {type(exc).__name__}: {exc}"
        return _finalize(result, "INFRASTRUCTURE_FAILURE", started)

    try:
        scratch = cache.scratch_repo_dir(repo)
        tree = _scratch_worktree(repo, base_commit, scratch)
        lock_path = _worktree_lock_path(ensure_repo_clone(repo), scratch)
        result["base_checkout"] = True
    except subprocess.TimeoutExpired as exc:
        # A checkout that exceeds even the generous budget is a host/filesystem
        # problem (typically an on-access virus scanner in the write path), not
        # a bad task. Never let it masquerade as CANDIDATE_INVALID.
        result["failure_reason"] = (
            f"checkout_timeout after {CHECKOUT_TIMEOUT}s: {exc}"
        )
        return _finalize(result, "TIMEOUT", started)
    except Exception as exc:  # noqa: BLE001
        result["failure_reason"] = f"checkout_failed: {type(exc).__name__}: {exc}"
        return _finalize(result, "CHECKOUT_FAILURE", started)

    gold_patch = swebench_source.load_patch(instance_id, "patch", repo)
    test_patch = swebench_source.load_patch(instance_id, "test_patch", repo)
    result["has_test_patch"] = bool(test_patch.strip())
    result["gold_patch_sha256"] = hashlib.sha256(
        gold_patch.encode("utf-8")
    ).hexdigest()

    # --- 1. gold patch must apply cleanly at base_commit ---------------------
    gold_out = _apply_patch_detail(tree, gold_patch, lock_path=lock_path)
    if not gold_out["applied"]:
        result["failure_reason"] = "gold_patch_does_not_apply"
        result["patch_stderr_tail"] = gold_out.get("stderr_tail")
        _reset_after_validation(tree, gold_patch, "", lock_path)
        return _finalize(result, "CANDIDATE_INVALID", started)
    result["gold_patch_applies"] = True

    # --- 2. test_patch must apply cleanly on top of it ----------------------
    # A missing test_patch is *not* success: SWE-bench tasks are defined by the
    # test-side change, so its absence is reported as its own failure reason
    # rather than silently skipping the tests that make the task meaningful.
    if not result["has_test_patch"]:
        result["failure_reason"] = "missing_test_patch"
        _reset_after_validation(tree, gold_patch, "", lock_path)
        return _finalize(result, "CANDIDATE_INVALID", started)
    test_out = _apply_patch_detail(tree, test_patch, lock_path=lock_path)
    if not test_out["applied"]:
        result["failure_reason"] = "test_patch_does_not_apply"
        result["test_patch_stderr_tail"] = test_out.get("stderr_tail")
        _reset_after_validation(tree, gold_patch, test_patch, lock_path)
        return _finalize(result, "CANDIDATE_INVALID", started)
    result["test_patch_applies"] = True

    # --- 3. measure the fixed state ------------------------------------------
    if run_tests:
        post = _run_tests(tree, interpreter, f2p)
        result["tests_pass_after_gold_patch"] = post["all_passed"]
        if not post["runnable"]:
            result["failure_reason"] = post["reason"]
            result["tests_stderr_tail"] = post.get("stderr_tail")
            _reset_after_validation(tree, gold_patch, test_patch, lock_path)
            return _finalize(result, post["bucket"], started)
        if not post["all_passed"]:
            # Distinguish a genuine assertion failure from a test that could not
            # even be collected/imported (a dependency or environment problem).
            if post.get("collection_error"):
                result["failure_reason"] = post["reason"]
                result["tests_stderr_tail"] = post.get("stderr_tail")
                _reset_after_validation(tree, gold_patch, test_patch, lock_path)
                return _finalize(result, "DEPENDENCY_FAILURE", started)
            result["failure_reason"] = "tests_do_not_pass_after_gold_patch"
            result["tests_stderr_tail"] = post.get("stderr_tail")
            _reset_after_validation(tree, gold_patch, test_patch, lock_path)
            return _finalize(result, "TEST_FAILURE", started)

        # --- 4. and confirm they genuinely fail without the gold fix ---------
        # This is the control that makes the task meaningful: if the tests pass
        # on the unpatched tree they cannot discriminate the fix.
        _revert_patch(tree, gold_patch, lock_path=lock_path)
        pre = _run_tests(tree, interpreter, f2p)
        result["tests_fail_before_gold_patch"] = pre["all_failed"]
        if not pre["runnable"]:
            result["failure_reason"] = pre["reason"]
            result["tests_stderr_tail"] = pre.get("stderr_tail")
            _reset_after_validation(tree, gold_patch, test_patch, lock_path)
            return _finalize(result, pre["bucket"], started)
        if pre["all_passed"]:
            result["failure_reason"] = "tests_pass_without_gold_patch"
            _reset_after_validation(tree, gold_patch, test_patch, lock_path)
            return _finalize(result, "CANDIDATE_INVALID", started)
        if not pre["all_failed"]:
            result["failure_reason"] = "tests_not_reproducible_before_gold_patch"
            _reset_after_validation(tree, gold_patch, test_patch, lock_path)
            return _finalize(result, "CANDIDATE_INVALID", started)

    # --- 5. leave the worktree pristine for the next candidate ---------------
    _reset_after_validation(tree, gold_patch, test_patch, lock_path)
    result["valid"] = True
    return _finalize(result, "VALID", started)


def _finalize(result: dict, classification: str, started: float) -> dict:
    """Stamp the duration + taxonomy bucket and return the result.

    Centralised so every early return reports a classification; the bucket is
    the single value the probe aggregates, which keeps the yield accounting
    from drifting between call sites.
    """
    result["duration_seconds"] = round(time.time() - started, 2)
    result["classification"] = classification
    return result


def _revert_patch(tree: Path, patch_text: str, *, lock_path: Path | None) -> bool:
    """Reverse a patch, tolerating an empty body."""
    if not patch_text.strip():
        return True
    return _apply_patch(tree, patch_text, reverse=True, lock_path=lock_path)


def _reset_after_validation(
    tree: Path, gold_patch: str, test_patch: str, lock_path: Path | None
) -> None:
    """Return the shared worktree to a clean HEAD after validating a candidate.

    Best-effort: a failure here must never change a validation verdict, because
    the next candidate's ``_scratch_worktree`` call repairs the tree anyway.
    """
    _revert_patch(tree, test_patch, lock_path=lock_path)
    _revert_patch(tree, gold_patch, lock_path=lock_path)
    try:
        _git_with_lock_retry("checkout", "--force", cwd=tree, lock_path=lock_path)
        _git_with_lock_retry("reset", "--hard", cwd=tree, lock_path=lock_path)
    except Exception:  # noqa: BLE001 - never propagate from cleanup
        pass


# SWE-bench stores test identifiers in unittest's *display* form,
# ``method (module.Class)``, while Django's ``runtests.py`` wants the importable
# ``module.Class.method`` form. Anything that is not one of those two shapes
# (SWE-bench also embeds stray prose in FAIL_TO_PASS) is dropped rather than
# guessed at.
_PAREN_LABEL_RE = re.compile(
    r"^\s*(?P<method>[A-Za-z_][\w.]*)\s*\(\s*(?P<cls>[A-Za-z_][\w.]*)\s*\)\s*$"
)
_DOTTED_LABEL_RE = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)+$")


def normalize_test_label(label: str) -> str | None:
    """Convert a SWE-bench test id to Django's ``module.Class.method`` form.

    Returns ``None`` for labels that are not test identifiers (prose fragments
    occur in the dataset), so callers can report them instead of inventing a
    bogus test path.
    """
    if not isinstance(label, str):
        return None
    text = label.strip()
    if not text:
        return None
    m = _PAREN_LABEL_RE.match(text)
    if m:
        return f"{m.group('cls')}.{m.group('method')}"
    if _DOTTED_LABEL_RE.match(text):
        return text
    return None


def normalize_test_labels(labels: list[str]) -> list[str]:
    """Normalize and de-duplicate labels, preserving order."""
    seen: dict[str, None] = {}
    for raw in labels:
        norm = normalize_test_label(raw)
        if norm:
            seen.setdefault(norm, None)
    return list(seen)


def _run_tests(tree: Path, interpreter: str, labels: list[str]) -> dict:
    """Run Django's test runner on the FAIL_TO_PASS labels.

    Django repos are evaluated with ``tests/runtests.py``. The checkout must be
    on ``sys.path`` (``runtests.py`` refuses to run otherwise), so
    ``PYTHONPATH`` is set to the worktree root.

    The returned dict always carries a ``bucket`` so the caller can map a
    non-runnable outcome onto the right taxonomy entry instead of defaulting to
    a test failure:

    - ``runnable=False`` + ``bucket="INFRASTRUCTURE_FAILURE"``: no entrypoint or
      no usable labels — our harness could not even attempt the run;
    - ``runnable=False`` + ``bucket="DEPENDENCY_FAILURE"``: the run started but
      the tests could not be collected/imported (missing package, wrong
      interpreter, broken import) — an environment problem, never a bad task;
    - ``runnable=False`` + ``bucket="TIMEOUT"``: exceeded ``TEST_TIMEOUT``;
    - ``runnable=True``: ``all_passed`` is the verdict.

    ``collection_error`` is the specific signal that separates "the assertion
    failed" from "the test never ran".
    """
    runner = tree / "runtests.py"
    if not runner.exists():
        runner = tree / "tests" / "runtests.py"
    if not runner.exists():
        return {"runnable": False, "reason": "no_runtests_entrypoint",
                "bucket": "INFRASTRUCTURE_FAILURE",
                "all_passed": None, "all_failed": None,
                "collection_error": False}
    normalized = normalize_test_labels(labels)
    if not normalized:
        # Every label was prose: there is no objective evaluator here. Say so
        # explicitly rather than running the whole suite by accident.
        return {"runnable": False, "reason": "no_runnable_test_labels",
                "bucket": "INFRASTRUCTURE_FAILURE",
                "all_passed": None, "all_failed": None,
                "collection_error": False}
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{tree}{os.pathsep}{existing}" if existing else str(tree)
    )
    args = [interpreter, str(runner), *normalized, "-v", "2", "--parallel", "1"]
    try:
        proc = _run(args, cwd=tree, timeout=TEST_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return {"runnable": False, "reason": "test_run_timeout",
                "bucket": "TIMEOUT",
                "all_passed": None, "all_failed": None,
                "collection_error": False}
    ok = proc.returncode == 0
    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    # Django prints a distinct banner when a test module cannot be imported.
    # That is an environment/dependency fault, not a failing assertion.
    collection_error = bool(
        re.search(
            r"(ImportError|ModuleNotFoundError|ZERO_TESTS|"
            r"Failed to import|Ran 0 tests|no such test)",
            combined,
        )
    ) and not ok
    return {
        "runnable": True,
        "reason": "test_collection_error" if collection_error else None,
        "bucket": "DEPENDENCY_FAILURE" if collection_error else "TEST_FAILURE",
        "all_passed": ok,
        "all_failed": (not ok),
        "collection_error": collection_error,
        "returncode": proc.returncode,
        "stdout_tail": (proc.stdout or "").strip()[-600:],
        "stderr_tail": (proc.stderr or "").strip()[-600:],
    }


def _result_path(repo_root: Path, instance_id: str) -> Path:
    return cache.validation_dir(repo_root) / "candidates" / f"{instance_id}.json"


def load_cached(repo_root: Path, instance_id: str) -> dict | None:
    path = _result_path(repo_root, instance_id)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def save_result(repo_root: Path, result: dict) -> Path:
    path = _result_path(repo_root, result["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return path


def candidate_fingerprint(candidate: dict) -> str:
    """Stable hash of the fields that affect validation outcome."""
    payload = json.dumps(
        {
            "instance_id": candidate["instance_id"],
            "base_commit": candidate["base_commit"],
            "gold_patch_sha256": candidate.get("gold_patch_sha256"),
            "test_spec": candidate.get("test_spec"),
            "version": candidate.get("version"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
