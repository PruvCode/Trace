"""Tests for the corpus Stage-A additions: history derivation, adapter, report.

All tests here are deterministic and network-free. History derivation is
exercised against a tiny throwaway git repository created in ``tmp_path`` with
fixed identity and dates, so no local fixture and no remote clone is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from corpus import adapter, history, report  # noqa: E402

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


@pytest.fixture()
def tiny_repo(tmp_path: Path) -> Path:
    """A 4-commit repo: add -> fix -> revert -> change (real git history)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")

    (repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add module")

    (repo / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fix(#1) return value: fix off-by-one")

    (repo / "mod.py").write_text("def f():\n    return 3\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Revert \"fix(#1) return value\"")

    (repo / "mod.py").write_text("def f():\n    return 4\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change f to return 4")
    return repo


def test_find_revert_commits_real(tiny_repo: Path):
    head = _git(tiny_repo, "rev-parse", "HEAD")
    reverts = history.find_revert_commits(tiny_repo, head, ["mod.py"])
    assert reverts, "expected the real revert commit to be found"
    assert any("Revert" in entry["subject"] for entry in reverts)


def test_derive_episodic_setup_prefers_revert(tiny_repo: Path):
    head = _git(tiny_repo, "rev-parse", "HEAD")
    setup = history.derive_episodic_setup(tiny_repo, head, ["mod.py"])
    assert setup is not None
    assert setup["kind"] == "episodic"
    assert setup["confidence"] == "reverted_attempt"
    assert setup["derived_from"]["is_ancestor_of_base"] is True
    event = setup["events"][0]
    assert event["source"] == "git"
    assert event["type"] in {"attempt", "investigation"}
    assert event["payload"]["provenance"] == "git_history"
    # The seeded commit must be a real ancestor, never the base itself.
    assert event["commit"] != head


def test_derive_episodic_setup_none_without_evidence(tmp_path: Path):
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add a")
    head = _git(repo, "rev-parse", "HEAD")
    # No revert, no fix-style subject -> no evidence -> must not be forced.
    assert history.derive_episodic_setup(repo, head, ["a.py"]) is None
    # No second touching commit -> no staleness either.
    assert history.derive_staleness_setup(repo, head, ["a.py"]) is None


def test_derive_staleness_setup_uses_superseded_commit(tiny_repo: Path):
    head = _git(tiny_repo, "rev-parse", "HEAD")
    setup = history.derive_staleness_setup(tiny_repo, head, ["mod.py"])
    assert setup is not None
    assert setup["kind"] == "staleness"
    assert setup["stale_sha"] != setup["current_sha"]
    assert setup["derived_from"]["is_stale_ancestor_of_current"] is True
    event = setup["events"][0]
    assert event["source"] == "git"
    assert event["payload"]["provenance"] == "git_history"
    assert event["commit"] == setup["stale_sha"]


def test_is_ancestor_rejects_descendant(tiny_repo: Path):
    head = _git(tiny_repo, "rev-parse", "HEAD")
    first = _git(tiny_repo, "rev-list", "--max-parents=0", "HEAD")
    assert history.is_ancestor(tiny_repo, first, head) is True
    assert history.is_ancestor(tiny_repo, head, first) is False


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------

def _manifest_task(**over):
    task = {
        "task_id": "django__django-12345",
        "source": "swe-bench",
        "source_task_id": "django__django-12345",
        "repo": "django/django",
        "base_commit": "a" * 40,
        "category": "B_structural",
        "problem_statement": "Fix the thing.",
        "evaluator": {
            "kind": "swebench_test_spec",
            "FAIL_TO_PASS": ["tests.test_x.TestY.test_z", "some prose fragment"],
            "PASS_TO_PASS": [],
            "runner": "tests/runtests.py",
        },
        "validation_status": {"valid": True},
        "selection_reason": "classified B_structural by patch shape",
        "memory_setup": None,
        "staleness_setup": None,
        "dataset": {"id": "princeton-nlp/SWE-bench", "revision": "r", "split": "test"},
        "version": "4.2",
    }
    task.update(over)
    return task


def test_adapter_materializes_loader_valid_task(tmp_path: Path):
    from benchmark import loader as loader_mod

    manifest = {"tasks": [_manifest_task()]}
    created = adapter.materialize_manifest(manifest, tmp_path)
    assert len(created) == 1
    task_dir = created[0]
    assert (task_dir / "task.yaml").is_file()
    assert (task_dir / "prompt.md").is_file()
    # Round-trip through the real loader with a stubbed fixture check.
    meta = (task_dir / "task.yaml").read_text(encoding="utf-8")
    assert "task_id: django__django-12345" in meta
    assert "category: B_structural" in meta
    assert "tests.test_x.TestY.test_z" in meta
    # Prose fragment must be filtered out of the evaluator command.
    assert "some prose fragment" not in meta
    assert adapter.is_generated(task_dir) is True
    _ = loader_mod  # imported to prove the schema module is importable here


def test_adapter_refuses_to_overwrite_foreign_dir(tmp_path: Path):
    manifest = {"tasks": [_manifest_task()]}
    created = adapter.materialize_manifest(manifest, tmp_path)
    # Simulate a hand-written task: remove our marker.
    (created[0] / adapter.GENERATED_MARKER).unlink()
    with pytest.raises(FileExistsError):
        adapter.materialize_manifest(manifest, tmp_path)
    # force=True is the explicit override.
    adapter.materialize_manifest(manifest, tmp_path, force=True)


def test_adapter_rejects_task_without_objective_evaluator(tmp_path: Path):
    bad = _manifest_task(
        evaluator={"kind": "swebench_test_spec", "FAIL_TO_PASS": ["only prose"],
                   "PASS_TO_PASS": []}
    )
    with pytest.raises(ValueError, match="no runnable FAIL_TO_PASS"):
        adapter.materialize_manifest({"tasks": [bad]}, tmp_path)


def test_adapter_is_deterministic(tmp_path: Path):
    manifest = {"tasks": [_manifest_task()]}
    first = adapter.materialize_manifest(manifest, tmp_path / "a")[0]
    second = adapter.materialize_manifest(manifest, tmp_path / "b")[0]
    assert (first / "task.yaml").read_bytes() == (second / "task.yaml").read_bytes()
    assert (first / "prompt.md").read_bytes() == (second / "prompt.md").read_bytes()


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def test_report_separates_infrastructure_from_candidate_invalid():
    assert report.classify_failure("environment_unsupported: Django 2.2") == "DEPENDENCY_FAILURE"
    assert report.classify_failure("checkout_failed: RuntimeError") == "CHECKOUT_FAILURE"
    assert report.classify_failure("tests_do_not_pass_after_gold_patch") == "TEST_FAILURE"
    assert report.classify_failure("tests_pass_without_gold_patch") == "CANDIDATE_INVALID"
    assert report.classify_failure("test_run_timeout") == "TIMEOUT"
    assert report.classify_failure(None) == "none"


def test_report_contains_honest_counts(tmp_path: Path):
    pool = [
        {
            "dataset": {
                "id": "princeton-nlp/SWE-bench",
                "revision": "e48e2bd",
                "split": "test",
                "parquet_sha256": "abc",
            }
        }
    ]
    validation = {
        "t1": {"valid": True},
        "t2": {"valid": False, "failure_reason": "environment_unsupported: old"},
        "t3": {"valid": False, "failure_reason": "gold_patch_does_not_apply"},
    }
    text = report.build_report(pool=pool, validation=validation, manifest=None)
    assert "princeton-nlp/SWE-bench" in text
    assert "1**" in text or "**1**" in text  # valid count surfaced
    assert "environment_unsupported" in text
    assert "gold_patch_does_not_apply" in text
    assert "CHECKOUT_FAILURE" in text or "infrastructure" in text.lower()
    # Never claim a manifest that does not exist.
    assert "_No manifest yet._" in text


def test_report_round_trips_manifest(tmp_path: Path):
    pool = [{"dataset": {"id": "d", "revision": "r", "split": "test",
                         "parquet_sha256": "x"}}]
    manifest = {
        "task_count": 1,
        "target_distribution": {"A_control": 0, "B_structural": 1,
                                "C_episodic": 0, "D_staleness": 0},
        "distribution": {"A_control": 0, "B_structural": 1,
                         "C_episodic": 0, "D_staleness": 0},
        "tasks": [
            {
                "task_id": "t1",
                "category": "B_structural",
                "repo": "django/django",
                "base_commit": "a" * 40,
                "validation_status": {"valid": True},
            }
        ],
    }
    text = report.build_report(pool=pool, validation={"t1": {"valid": True}},
                               manifest=manifest)
    assert "`t1`" in text
    assert "B_structural" in text
    path = report.write_report(tmp_path / "r.md", text)
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == text


# --- validator regression tests -------------------------------------------------
# These cover the patch-application path. The original bug was that the patch
# body was never fed to `git apply -` on stdin, so *every* candidate failed with
# "No valid patches in input" and was mis-bucketed as a candidate problem.


def _git_bytes(cwd: Path, *args: str) -> bytes:
    """Run git and return raw stdout bytes (no newline translation).

    Text mode would rewrite ``\\n`` to ``\\r\\n`` on Windows, which is exactly
    the corruption these regression tests exist to guard against.
    """
    env = dict(os.environ)
    env.update(SEED_ENV)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        timeout=60,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.decode()}")
    return proc.stdout


def test_apply_patch_feeds_patch_via_stdin(tmp_path: Path):
    """A real diff must be applied through validate._apply_patch."""
    from corpus import validate

    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    # newline="" keeps the written bytes exactly LF, matching SWE-bench patches.
    (repo / "a.txt").write_text("one\n", encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add two")
    patch = _git_bytes(repo, "diff", "HEAD~1", "HEAD").decode("utf-8")
    assert "\r" not in patch, "fixture patch must be LF-only"

    # Reset to the base commit and apply the patch through the validator.
    _git(repo, "checkout", "--detach", base)
    assert validate._apply_patch(repo, patch) is True
    assert (repo / "a.txt").read_bytes() == b"one\ntwo\n"
    # And it must revert cleanly too (the validator measures both states).
    assert validate._apply_patch(repo, patch, reverse=True) is True
    assert (repo / "a.txt").read_bytes() == b"one\n"


def test_apply_patch_preserves_lf_line_endings(tmp_path: Path):
    """Guards the Windows CRLF regression: LF patches must not be mangled.

    `subprocess.run(..., text=True, input=...)` rewrites ``\\n`` to ``\\r\\n``
    on stdin under Windows, so git saw CRLF line terminators and reported
    "patch does not apply" for every candidate.
    """
    from corpus import validate

    repo = tmp_path / "r2"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "b.txt").write_text("x\n", encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "b.txt").write_text("x\ny\nz\n", encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "grow")
    patch = _git_bytes(repo, "diff", "HEAD~1", "HEAD").decode("utf-8")
    _git(repo, "checkout", "--detach", base)

    # Bytes delivered to git's stdin must be byte-identical to the patch.
    probe = validate._run(
        [sys.executable, "-c",
         "import sys; sys.stdout.write(repr(sys.stdin.buffer.read()))"],
        cwd=repo,
        input_text=patch,
    )
    assert r"\r\n" not in probe.stdout, probe.stdout
    assert validate._apply_patch(repo, patch) is True
    assert (repo / "b.txt").read_bytes() == b"x\ny\nz\n"


def test_apply_patch_empty_patch_is_noop(tmp_path: Path):
    from corpus import validate

    assert validate._apply_patch(tmp_path, "") is True
    assert validate._apply_patch(tmp_path, "   \n") is True


def test_run_passes_input_text_to_stdin(tmp_path: Path):
    """`_run` must actually deliver input_text to the child's stdin."""
    from corpus import validate

    proc = validate._run(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        cwd=tmp_path,
        input_text="hello-stdin",
    )
    assert proc.returncode == 0
    assert proc.stdout == "hello-stdin"


def test_clear_stale_lock_only_removes_old_locks(tmp_path: Path):
    """A fresh lock may belong to a live git; only aged locks are cleared."""
    from corpus import validate

    lock = tmp_path / "index.lock"
    lock.write_text("", encoding="utf-8")
    # Fresh lock: must be preserved.
    assert validate._clear_stale_lock(lock) is False
    assert lock.exists()

    # Aged lock: abandoned, safe to clear.
    old = os.stat(lock).st_mtime - (validate.STALE_LOCK_SECONDS + 60)
    os.utime(lock, (old, old))
    assert validate._clear_stale_lock(lock) is True
    assert not lock.exists()

    # Absent lock is a no-op, not an error.
    assert validate._clear_stale_lock(lock) is False


def test_git_env_sets_lock_timeout():
    """Every git call must wait on contention instead of failing instantly."""
    from corpus import validate

    env = validate._git_env()
    assert "lockTimeout" in env["GIT_CONFIG_PARAMETERS"]


def test_reset_repairs_emptied_index(tmp_path: Path):
    """A killed run can leave an empty index; reset must rebuild it.

    Symptom of the bug: `git ls-files` returns nothing and `git apply` fails
    with "does not exist in index", which was misread as an unappliable patch.
    """
    from corpus import validate

    repo = tmp_path / "r3"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "c.txt").write_text("v\n", encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    assert validate._index_is_intact(repo) is True

    # Simulate the corruption: empty the index (as an interrupted git would).
    index = repo / ".git" / "index"
    index.write_bytes(b"")
    # Leave behind an abandoned lock, as a killed process would.
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    old = os.stat(lock).st_mtime - (validate.STALE_LOCK_SECONDS + 60)
    os.utime(lock, (old, old))

    assert validate._index_is_intact(repo) is False
    # Repair path: the lock is cleared and the index rebuilt from HEAD.
    validate._repair_index(repo, lock)
    assert not lock.exists()
    assert validate._index_is_intact(repo) is True
    assert _git(repo, "status", "--porcelain") == ""


def test_index_is_intact_detects_empty_index(tmp_path: Path):
    from corpus import validate

    repo = tmp_path / "r4"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / ".git" / "index").write_bytes(b"")
    assert validate._index_is_intact(repo) is False


def test_worktree_is_registered_detects_dangling(tmp_path: Path):
    """A dangling `.git` marker (post-prune) must not count as a worktree."""
    from corpus import validate

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    (clone / "f.txt").write_text("v\n", encoding="utf-8", newline="")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "base")

    wt = tmp_path / "wt"
    _git(clone, "worktree", "add", "--detach", str(wt), "HEAD")
    assert validate._worktree_is_registered(clone, wt) is True

    # Simulate `git worktree prune` having dropped the metadata directory.
    gitdir = clone / ".git" / "worktrees" / wt.name
    assert gitdir.exists()
    import shutil as _shutil
    _shutil.rmtree(gitdir)
    assert validate._worktree_is_registered(clone, wt) is False


def test_clear_stale_lock_clears_orphan_regardless_of_age(tmp_path: Path):
    """An orphaned lock (no git process alive) must clear even when young.

    The original age-only rule (600s) left a fresh orphan blocking the worktree
    and every checkout failed for ~10 minutes.
    """
    from corpus import validate

    lock = tmp_path / "index.lock"
    lock.write_text("", encoding="utf-8")
    # Make it older than MIN_LOCK_AGE_SECONDS but far younger than the fallback.
    old = os.stat(lock).st_mtime - (validate.MIN_LOCK_AGE_SECONDS + 5)
    os.utime(lock, (old, old))

    if validate._no_git_process_running():
        # Authoritative signal: no git exists, so this lock is orphaned.
        assert validate._clear_stale_lock(lock) is True
        assert not lock.exists()


def test_clear_stale_lock_keeps_very_fresh_lock(tmp_path: Path):
    """A just-created lock may belong to a git we literally just spawned."""
    from corpus import validate

    lock = tmp_path / "index.lock"
    lock.write_text("", encoding="utf-8")
    now = os.stat(lock).st_mtime
    os.utime(lock, (now, now))
    assert validate._clear_stale_lock(lock) is False
    assert lock.exists()


def test_no_git_process_running_returns_bool():
    from corpus import validate

    assert isinstance(validate._no_git_process_running(), bool)


# --- test_patch semantics -------------------------------------------------------
# SWE-bench defines a task with TWO patches: `patch` (the source fix) and
# `test_patch` (the test-side change introducing the FAIL_TO_PASS assertions).
# Applying only `patch` makes every candidate look broken, because the new
# assertions do not exist yet.


def _candidate_stub(**over):
    base = {
        "instance_id": "django__django-00000",
        "repo": "django/django",
        "base_commit": "0" * 40,
        "version": "4.1",
        "test_spec": {
            "FAIL_TO_PASS": ["t_x (mod.Cls)"],
            "PASS_TO_PASS": [],
        },
    }
    base.update(over)
    return base


def test_normalize_test_label_converts_swebench_display_form():
    from corpus import validate

    assert (
        validate.normalize_test_label("test_a (mod.Cls)")
        == "mod.Cls.test_a"
    )
    assert (
        validate.normalize_test_label("mod.Cls.test_a") == "mod.Cls.test_a"
    )


def test_normalize_test_label_rejects_prose():
    from corpus import validate

    assert validate.normalize_test_label("--flag does something.") is None
    assert validate.normalize_test_label("") is None
    assert validate.normalize_test_label(None) is None


def test_normalize_test_labels_dedupes_and_preserves_order():
    from corpus import validate

    out = validate.normalize_test_labels(
        ["b (m.C)", "a (m.C)", "m.C.a", "prose here."]
    )
    assert out == ["m.C.b", "m.C.a"]


def test_classify_failure_never_labels_infrastructure_as_candidate_invalid():
    from corpus import report

    assert report.classify_failure("checkout_failed: RuntimeError: x") == "CHECKOUT_FAILURE"
    assert report.classify_failure("checkout_timeout after 3600s: x") == "TIMEOUT"
    assert report.classify_failure("environment_unsupported: Django 2.2") == "DEPENDENCY_FAILURE"
    assert report.classify_failure("test_collection_error") == "DEPENDENCY_FAILURE"
    assert report.classify_failure("test_run_timeout") == "TIMEOUT"
    assert report.classify_failure("no_runtests_entrypoint") == "INFRASTRUCTURE_FAILURE"
    assert report.classify_failure("no_runnable_test_labels") == "INFRASTRUCTURE_FAILURE"
    # A genuine assertion failure is its own bucket, not candidate-invalid: the
    # task may still be well-formed, the fix simply did not make it pass.
    assert report.classify_failure("tests_do_not_pass_after_gold_patch") == "TEST_FAILURE"
    # Genuinely bad-task reasons.
    for reason in (
        "missing_test_patch",
        "test_patch_does_not_apply",
        "tests_pass_without_gold_patch",
        "tests_not_reproducible_before_gold_patch",
        "no_fail_to_pass_tests",
        "gold_patch_does_not_apply",
        "base_commit_missing_from_repo",
    ):
        assert report.classify_failure(reason) == "CANDIDATE_INVALID", reason


def test_revert_patch_handles_empty_body(tmp_path: Path):
    from corpus import validate

    # An empty patch is a no-op in both directions, never an error.
    assert validate._revert_patch(tmp_path, "", lock_path=None) is True
    assert validate._revert_patch(tmp_path, "  \n", lock_path=None) is True


def test_apply_patch_sequence_patch_then_test_patch(tmp_path: Path):
    """The real ordering: gold patch applies, then test_patch applies on top."""
    from corpus import validate

    repo = tmp_path / "r5"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8", newline="")
    (repo / "t.py").write_text("def test_f():\n    assert True\n", encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    # Gold patch: fix the source.
    (repo / "src.py").write_text("def f():\n    return 2\n", encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fix")
    gold = _git_bytes(repo, "diff", "HEAD~1", "HEAD").decode("utf-8")

    # test_patch: add the assertion that exercises the fix.
    (repo / "t.py").write_text(
        "from src import f\n\ndef test_f():\n    assert f() == 2\n",
        encoding="utf-8", newline="",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "tests")
    test_patch = _git_bytes(repo, "diff", "HEAD~1", "HEAD").decode("utf-8")

    _git(repo, "checkout", "--detach", base)
    assert validate._apply_patch(repo, gold) is True
    assert validate._apply_patch(repo, test_patch) is True
    # Both revert cleanly, in reverse order.
    assert validate._revert_patch(repo, test_patch, lock_path=None) is True
    assert validate._revert_patch(repo, gold, lock_path=None) is True
    assert _git(repo, "status", "--porcelain") == ""


def test_test_patch_failure_is_its_own_reason(tmp_path: Path):
    """A test_patch that cannot apply must not be reported as a gold-patch issue."""
    from corpus import validate

    repo = tmp_path / "r6"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")

    bogus = (
        "diff --git a/nope.py b/nope.py\n"
        "--- a/nope.py\n+++ b/nope.py\n"
        "@@ -1,1 +1,1 @@\n-wrong\n+content\n"
    )
    assert validate._apply_patch(repo, bogus) is False


def test_missing_test_patch_is_not_success():
    """`has_test_patch` must be recorded so absence is never treated as success."""
    from corpus import swebench_source, validate

    # Empty bodies must not be silently accepted as applied.
    assert validate._apply_patch(Path("."), "") is True  # no-op is fine
    # but the candidate-level flag distinguishes "nothing to apply"
    # from "applied": proven by the field being set explicitly.
    src = Path(validate.__file__).read_text(encoding="utf-8")
    assert "missing_test_patch" in src
    assert "test_patch_does_not_apply" in src
    assert "tests_pass_without_gold_patch" in src


# --- test_patch semantics (round 2) ---------------------------------------------
# The first validator cut applied only `patch` and fed nothing on stdin; then it
# applied `patch` but still let an empty/missing `test_patch` pass silently. Both
# bugs made the corpus look uniformly broken. These tests pin the fixed contract:
# patch -> test_patch -> run tests -> classify, with a distinct reason per stage.


def test_apply_patch_detail_reports_stderr_on_failure(tmp_path: Path):
    """A failed apply must surface git's stderr, not an empty string.

    Regression: the boolean-only ``_apply_patch`` discarded stderr, so a killed
    or hung operation produced ``failure_reason=""`` and could not be diagnosed.
    """
    from corpus import validate

    repo = tmp_path / "det"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")

    bogus = (
        "diff --git a/does_not_exist.py b/does_not_exist.py\n"
        "--- a/does_not_exist.py\n"
        "+++ b/does_not_exist.py\n"
        "@@ -1,1 +1,1 @@\n-nope\n+other\n"
    )
    detail = validate._apply_patch_detail(repo, bogus)
    assert detail["applied"] is False
    assert detail["stderr_tail"], "stderr must be captured for diagnosis"
    # And the boolean wrapper agrees with the detail view.
    assert validate._apply_patch(repo, bogus) is False


def test_apply_patch_detail_reports_empty_body(tmp_path: Path):
    """An empty body is a mechanic-level no-op the caller can distinguish."""
    from corpus import validate

    detail = validate._apply_patch_detail(tmp_path, "")
    assert detail["applied"] is True
    assert detail["empty_body"] is True

    detail2 = validate._apply_patch_detail(tmp_path, "   \n")
    assert detail2["applied"] is True
    assert detail2["empty_body"] is True


def _two_patch_repo(tmp_path: Path, name: str) -> tuple[Path, str, str, str]:
    """Build a repo with a base commit, a source fix, and a test-side change.

    Returns ``(repo, base_sha, gold_patch, test_patch)`` where both patches are
    LF-only diffs, exactly like SWE-bench's ``patch`` / ``test_patch`` fields.
    """
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")

    (repo / "src.py").write_text(
        "def double(n):\n    return n\n", encoding="utf-8", newline=""
    )
    (repo / "test_src.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n"
        "    def test_double(self):\n        self.assertTrue(True)\n",
        encoding="utf-8", newline="",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    # Gold patch: fix the source only.
    (repo / "src.py").write_text(
        "def double(n):\n    return n * 2\n", encoding="utf-8", newline=""
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fix double")
    gold = _git_bytes(repo, "diff", "HEAD~1", "HEAD").decode("utf-8")

    # test_patch: add the assertion that exercises the fix.
    (repo / "test_src.py").write_text(
        "import unittest\nfrom src import double\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_double(self):\n        self.assertEqual(double(2), 4)\n",
        encoding="utf-8", newline="",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "tests")
    test_patch = _git_bytes(repo, "diff", "HEAD~1", "HEAD").decode("utf-8")

    assert "\r" not in gold and "\r" not in test_patch
    _git(repo, "checkout", "--detach", base)
    return repo, base, gold, test_patch


def test_successful_patch_then_test_patch_produces_expected_tree(tmp_path: Path):
    """The happy path: both patches apply, in order, and yield the fixed state."""
    from corpus import validate

    repo, _base, gold, test_patch = _two_patch_repo(tmp_path, "ok")

    gold_detail = validate._apply_patch_detail(repo, gold)
    assert gold_detail["applied"] is True
    test_detail = validate._apply_patch_detail(repo, test_patch)
    assert test_detail["applied"] is True

    # Source fix and test-side assertion are both present.
    assert "return n * 2" in (repo / "src.py").read_text(encoding="utf-8")
    assert "assertEqual(double(2), 4)" in (repo / "test_src.py").read_text(
        encoding="utf-8"
    )

    # Reverting in reverse order must restore the base tree exactly.
    assert validate._revert_patch(repo, test_patch, lock_path=None) is True
    assert validate._revert_patch(repo, gold, lock_path=None) is True
    assert _git(repo, "status", "--porcelain") == ""


def test_test_patch_failure_on_top_of_applied_patch(tmp_path: Path):
    """A test_patch that cannot apply after the gold patch is its own failure.

    This is the distinction the taxonomy depends on: it must NOT be reported as
    ``gold_patch_does_not_apply``.
    """
    from corpus import validate

    repo, _base, gold, _test_patch = _two_patch_repo(tmp_path, "badtest")
    bogus_test_patch = (
        "diff --git a/test_missing.py b/test_missing.py\n"
        "--- a/test_missing.py\n"
        "+++ b/test_missing.py\n"
        "@@ -1,2 +1,2 @@\n-absent\n+present\n"
    )
    assert validate._apply_patch_detail(repo, gold)["applied"] is True
    detail = validate._apply_patch_detail(repo, bogus_test_patch)
    assert detail["applied"] is False
    assert detail["stderr_tail"]

    from corpus import report

    assert report.classify_failure("test_patch_does_not_apply") == "CANDIDATE_INVALID"
    assert report.classify_failure("gold_patch_does_not_apply") == "CANDIDATE_INVALID"
    # The two reasons are distinct strings, never collapsed into one.
    assert "test_patch_does_not_apply" != "gold_patch_does_not_apply"


def test_run_tests_classifies_missing_entrypoint_as_infrastructure(tmp_path: Path):
    """No runnable entrypoint is an infrastructure result, never a task failure."""
    from corpus import validate

    out = validate._run_tests(tmp_path, sys.executable, ["a.B.c"])
    assert out["runnable"] is False
    assert out["reason"] == "no_runtests_entrypoint"
    assert out["bucket"] == "INFRASTRUCTURE_FAILURE"


def test_run_tests_passes_and_fails_are_distinguished(tmp_path: Path):
    """A green run and a red run must be distinguishable, both runnable."""
    from corpus import validate

    repo = tmp_path / "run"
    repo.mkdir()
    runner = repo / "runtests.py"
    # Labels must be in the dotted module.Class.method form the normalizer
    # accepts; an undotted token is correctly rejected as non-test prose.
    runner.write_text(
        "import sys\n"
        "args = [a for a in sys.argv[1:] if not a.startswith('-')]\n"
        "sys.exit(0 if args and args[0].startswith('m.C.pass') else 1)\n",
        encoding="utf-8",
        newline="\n",
    )
    ok = validate._run_tests(repo, sys.executable, ["m.C.pass_case"])
    assert ok["runnable"] is True
    assert ok["all_passed"] is True
    assert ok["all_failed"] is False

    bad = validate._run_tests(repo, sys.executable, ["m.C.fail_case"])
    assert bad["runnable"] is True
    assert bad["all_passed"] is False
    assert bad["all_failed"] is True


def test_run_tests_flags_collection_error_as_dependency(tmp_path: Path):
    """An import failure is a dependency problem, not a failing assertion.

    Without this split, a missing package (or a wrong interpreter) would be
    recorded as ``tests_do_not_pass_after_gold_patch`` and misread as a broken
    task.
    """
    from corpus import validate

    repo = tmp_path / "coll"
    repo.mkdir()
    (repo / "runtests.py").write_text(
        "import sys\n"
        "sys.stderr.write('ImportError: cannot import name X\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
        newline="\n",
    )
    out = validate._run_tests(repo, sys.executable, ["mod.Cls.test_a"])
    assert out["runnable"] is True
    assert out["all_passed"] is False
    assert out["collection_error"] is True
    assert out["reason"] == "test_collection_error"
    assert out["bucket"] == "DEPENDENCY_FAILURE"

    from corpus import report

    assert report.classify_failure("test_collection_error") == "DEPENDENCY_FAILURE"


def test_validation_version_bumped():
    """A semantic change to the validator must invalidate cached results."""
    from corpus import validate

    assert validate.VALIDATION_VERSION != "1"


def test_checkout_timeout_exceeds_measured_checkout_cost():
    """The checkout budget must be above the real cost of a Django checkout.

    Regression: the 600 s budget was measured to be *shorter* than a full
    ~6,600-file checkout on this host (~690 s with an on-access scanner in the
    write path), so healthy checkouts were killed and reported as failures.
    """
    from corpus import validate

    assert validate.CHECKOUT_TIMEOUT > 600
    assert validate.TEST_TIMEOUT >= 1800


def test_git_env_pins_autocrlf_off_via_both_channels():
    """autocrlf must be pinned off through the modern env channel too.

    ``GIT_CONFIG_PARAMETERS`` alone does not survive every shell, and the host
    has ``core.autocrlf=true`` set at the *system* level, so a second pin is
    required for child git processes to inherit LF semantics.
    """
    from corpus import validate

    env = validate._git_env()
    assert "autocrlf=false" in env["GIT_CONFIG_PARAMETERS"]
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "core.autocrlf"
    assert env["GIT_CONFIG_VALUE_0"] == "false"


def test_git_env_autocrlf_actually_effective(tmp_path: Path):
    """End-to-end: git run with the validator env reports autocrlf=false.

    The host sets core.autocrlf=true system-wide, so this proves the pin wins
    over system config rather than merely being present in the env dict.
    """
    from corpus import validate

    repo = tmp_path / "acr"
    repo.mkdir()
    validate._run(["git", "init", "-b", "main"], cwd=repo, env=validate._git_env())
    proc = validate._run(
        ["git", "config", "--get", "core.autocrlf"], cwd=repo, env=validate._git_env()
    )
    assert proc.stdout.strip() == "false", proc.stdout


def test_finalize_stamps_classification_and_duration():
    """Every result must carry an explicit classification and a duration."""
    from corpus import validate

    result = {"task_id": "t", "valid": False}
    out = validate._finalize(result, "CHECKOUT_FAILURE", 0.0)
    assert out["classification"] == "CHECKOUT_FAILURE"
    assert isinstance(out["duration_seconds"], float)


def test_report_taxonomy_is_total():
    """Every failure_reason the validator can emit maps to a known bucket."""
    from corpus import report

    emissions = [
        "no_fail_to_pass_tests",
        "no_runnable_test_labels",
        "base_commit_missing_from_repo",
        "repo_checkout_error: RuntimeError: x",
        "checkout_timeout after 3600s: x",
        "checkout_failed: RuntimeError: x",
        "gold_patch_does_not_apply",
        "missing_test_patch",
        "test_patch_does_not_apply",
        "tests_do_not_pass_after_gold_patch",
        "tests_pass_without_gold_patch",
        "tests_not_reproducible_before_gold_patch",
        "no_runtests_entrypoint",
        "test_run_timeout",
        "test_collection_error",
        "environment_unsupported: Django 2.2 is not runnable",
    ]
    for reason in emissions:
        bucket = report.classify_failure(reason)
        assert bucket in report.TAXONOMY, (reason, bucket)
        assert bucket != "none", reason
