"""Corpus pipeline tests: ingestion, classification, manifest, caching.

Deterministic and network-free: a synthetic parquet fixture stands in for the
pinned SWE-bench dataset, and the validation cache is exercised through the
pure helpers. Real dataset ingestion is a manual step (scripts/corpus.py).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from corpus import cache, manifest as manifest_mod, swebench_source, validate  # noqa: E402


def _write_synthetic_parquet(path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    table = pa.table(
        {
            "repo": ["django/django", "django/django", "sympy/sympy"],
            "instance_id": [
                "django__django-1",
                "django__django-2",
                "sympy__sympy-9",
            ],
            "base_commit": ["a" * 40, "b" * 40, "c" * 40],
            "patch": [
                "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n",
                (
                    "diff --git a/y.py b/y.py\n--- a/y.py\n+++ b/y.py\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                    "diff --git a/z.py b/z.py\n--- a/z.py\n+++ b/z.py\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                ),
                "diff --git a/s.py b/s.py\n",
            ],
            "test_patch": ["", "", ""],
            "problem_statement": ["fix x", "fix y and z", "fix s"],
            "hints_text": ["", "", ""],
            "created_at": ["2020-01-01T00:00:00Z"] * 3,
            "version": ["4.2", "5.0", "1.0"],
            "FAIL_TO_PASS": [
                json.dumps(["tests.test_x"]),
                json.dumps(["tests.test_y", "tests.test_z"]),
                json.dumps([]),
            ],
            "PASS_TO_PASS": [json.dumps([]), json.dumps([]), json.dumps([])],
            "environment_setup_commit": ["e" * 40, "e" * 40, "e" * 40],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


@pytest.fixture()
def patched_cache(tmp_path, monkeypatch):
    """Point the cache at a temp root with a synthetic parquet in place."""
    root = tmp_path / "cache"
    monkeypatch.setenv("TRACE_SWE_CACHE", str(root))
    parquet = root / "datasets" / "princeton-nlp__SWE-bench" / "test.parquet"
    _write_synthetic_parquet(parquet)
    return root


def test_cache_root_honours_env(patched_cache):
    assert cache.cache_root() == patched_cache.resolve()
    assert cache.dataset_parquet_path().exists()


def test_ingest_filters_repo_and_preserves_fields(patched_cache):
    candidates = swebench_source.extract_candidates("django/django", ensure=False)
    assert [c["instance_id"] for c in candidates] == [
        "django__django-1",
        "django__django-2",
    ]
    first = candidates[0]
    for field in (
        "source",
        "source_task_id",
        "repo",
        "base_commit",
        "problem_statement",
        "test_spec",
        "gold_patch_sha256",
        "dataset",
    ):
        assert field in first
    assert first["dataset"]["revision"] == cache.DATASET_REVISION
    # Patches are referenced, not inlined (no upstream duplication).
    assert "patch" not in first


def test_pool_round_trip(patched_cache, tmp_path):
    candidates = swebench_source.extract_candidates("django/django", ensure=False)
    out = tmp_path / "pool.jsonl"
    swebench_source.write_candidate_pool(candidates, out)
    assert swebench_source.read_candidate_pool(out) == candidates


def test_patch_shape_and_classify():
    single = "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    multi = single + "diff --git a/y.py b/y.py\n@@ -1 +1 @@\n-a\n+b\n"
    shape = manifest_mod.patch_shape(single)
    assert shape["file_count"] == 1
    assert shape["files"] == ["x.py"]

    small = {
        "instance_id": "c1",
        "test_spec": {"FAIL_TO_PASS": ["t1"], "PASS_TO_PASS": []},
    }
    big = {
        "instance_id": "c2",
        "test_spec": {"FAIL_TO_PASS": ["t1", "t2", "t3", "t4"], "PASS_TO_PASS": []},
    }
    assert manifest_mod.classify(small, single) == "A_control"
    assert manifest_mod.classify(big, multi) == "B_structural"
    # Real git evidence outranks patch shape.
    assert manifest_mod.classify(big, multi, has_prior_history=True) == "C_episodic"
    assert (
        manifest_mod.classify(big, multi, has_stale_predecessor=True) == "D_staleness"
    )


def test_build_manifest_respects_quotas_and_schema():
    candidates = []
    validation = {}
    gold = {}
    for i in range(3):
        iid = f"django__django-{i}"
        candidates.append(
            {
                "instance_id": iid,
                "source": "swe-bench",
                "source_task_id": iid,
                "repo": "django/django",
                "base_commit": "a" * 40,
                "problem_statement": f"fix {i}",
                "test_spec": {"FAIL_TO_PASS": ["t1"], "PASS_TO_PASS": []},
                "dataset": {"id": "d", "revision": "r", "split": "test"},
            }
        )
        validation[iid] = {"valid": True, "base_checkout": True,
                           "gold_patch_applies": True, "tests_available": True,
                           "tests_pass_after_gold_patch": True, "failure_reason": None}
        # Candidates 0 and 1 are single-file (A_control); candidate 2 is a
        # two-file change so it classifies as B_structural.
        single = "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        gold[iid] = single if i < 2 else (
            single + "diff --git a/y.py b/y.py\n@@ -1 +1 @@\n-a\n+b\n"
        )

    built = manifest_mod.build_manifest(
        candidates, validation, gold, quotas={"A_control": 2, "B_structural": 1,
                                              "C_episodic": 0, "D_staleness": 0}
    )
    assert built["task_count"] == 3
    assert built["distribution"] == {
        "A_control": 2,
        "B_structural": 1,
        "C_episodic": 0,
        "D_staleness": 0,
    }
    assert manifest_mod.validate_manifest(built) == []


def test_build_manifest_skips_invalid_candidates():
    candidates = [
        {
            "instance_id": "bad",
            "source": "swe-bench",
            "source_task_id": "bad",
            "repo": "django/django",
            "base_commit": "a" * 40,
            "problem_statement": "x",
            "test_spec": {"FAIL_TO_PASS": ["t"], "PASS_TO_PASS": []},
            "dataset": {"id": "d", "revision": "r", "split": "test"},
        }
    ]
    built = manifest_mod.build_manifest(
        candidates,
        {"bad": {"valid": False, "failure_reason": "gold_patch_does_not_apply"}},
        {},
        quotas={"A_control": 1, "B_structural": 0, "C_episodic": 0, "D_staleness": 0},
    )
    assert built["task_count"] == 0


def test_validate_manifest_detects_duplicates():
    bad = {
        "tasks": [
            {"task_id": "x", "category": "A_control"},
            {"task_id": "x", "category": "A_control"},
        ]
    }
    problems = manifest_mod.validate_manifest(bad)
    assert any("duplicate" in p for p in problems)
    assert any("missing" in p for p in problems)


def test_python_for_version_gating():
    assert validate._python_for_version("5.0") == sys.executable
    assert validate._python_for_version("4.2") == sys.executable
    assert validate._python_for_version("3.2") is None
    assert validate._python_for_version(None) is None


def test_candidate_fingerprint_is_stable():
    candidate = {
        "instance_id": "x",
        "base_commit": "a" * 40,
        "gold_patch_sha256": "h",
        "test_spec": {"FAIL_TO_PASS": ["t"]},
        "version": "4.2",
    }
    assert validate.candidate_fingerprint(candidate) == validate.candidate_fingerprint(
        dict(candidate)
    )


def test_validation_result_round_trip(tmp_path):
    result = {
        "task_id": "django__django-1",
        "valid": True,
        "base_checkout": True,
        "gold_patch_applies": True,
        "tests_available": True,
        "tests_pass_after_gold_patch": True,
        "failure_reason": None,
    }
    path = validate.save_result(tmp_path, result)
    assert path.exists()
    assert validate.load_cached(tmp_path, "django__django-1")["valid"] is True
    assert validate.load_cached(tmp_path, "missing") is None


def test_validate_candidate_rejects_empty_test_spec(patched_cache):
    result = validate.validate_candidate(
        {
            "instance_id": "nope",
            "repo": "django/django",
            "base_commit": "a" * 40,
            "test_spec": {"FAIL_TO_PASS": [], "PASS_TO_PASS": []},
            "version": "5.0",
        },
        run_tests=False,
    )
    assert result["valid"] is False
    assert result["failure_reason"] == "no_fail_to_pass_tests"


def test_validate_candidate_flags_unsupported_environment(patched_cache):
    result = validate.validate_candidate(
        {
            "instance_id": "old",
            "repo": "django/django",
            "base_commit": "a" * 40,
            "test_spec": {"FAIL_TO_PASS": ["t"], "PASS_TO_PASS": []},
            "version": "2.2",
        },
        run_tests=True,
        allow_network=False,
    )
    # Either the repo cache is absent (network disabled) or the environment is
    # unsupported; both are honest, non-fabricated failure reasons.
    assert result["valid"] is False
    assert result["failure_reason"].startswith(
        ("environment_unsupported", "repo_checkout_error")
    )


def test_scratch_worktree_avoids_delete_throttled_appdata(
    patched_cache, monkeypatch, tmp_path
):
    """The worktree must NOT live under AppData\\Local when TEMP is available.

    A Django checkout deletes ~6,600 files when switching commits. Deletion
    under %LOCALAPPDATA% is throttled to ~3 files/s by a filesystem filter
    (~300x slower than %TEMP%), which turned a ~12s worktree add into a 3600s+
    timeout. The worktree therefore belongs on the fast path; the read-only
    object store stays in the cache.
    """
    fake_temp = tmp_path / "fasttemp"
    fake_temp.mkdir()
    monkeypatch.setenv("TEMP", str(fake_temp))
    monkeypatch.setenv("TMP", str(fake_temp))

    scratch = cache.scratch_repo_dir("django/django")
    assert "TraceSWECache" not in str(scratch), scratch
    # Layout is <TEMP>/trace_scratch/<repo>
    assert scratch.parent.name == "trace_scratch"
    assert scratch.name == "django__django"
    assert str(scratch).startswith(str(fake_temp.resolve()))


def test_scratch_worktree_honours_explicit_override(patched_cache, monkeypatch, tmp_path):
    """TRACE_SWE_SCRATCH wins over the platform default."""
    target = tmp_path / "explicit" / "location"
    monkeypatch.setenv("TRACE_SWE_SCRATCH", str(target))

    scratch = cache.scratch_repo_dir("django/django")
    assert scratch == target.resolve() / "django__django"


def test_scratch_worktree_stays_one_per_repo(patched_cache, monkeypatch, tmp_path):
    """Exactly one reusable worktree per repo (guard-safe contract)."""
    monkeypatch.setenv("TEMP", str(tmp_path / "t"))
    a = cache.scratch_repo_dir("django/django")
    b = cache.scratch_repo_dir("django/django")
    assert a == b
    assert cache.scratch_repo_dir("pallets/flask") != a
