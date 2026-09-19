"""CLI for the TRACE SWE-bench corpus pipeline.

Subcommands:
    ingest   dataset -> candidate pool (JSONL)
    validate pool -> per-candidate validation JSON (cached)
    build    validated pool -> manifest.json + selection report
    status   summarize what exists on disk

Deterministic and re-runnable: ingest is pinned to a dataset revision,
validation is cached per instance id, and build is a pure function of the
pool + validation results.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from corpus import cache, manifest as manifest_mod  # noqa: E402
from corpus import adapter as adapter_mod  # noqa: E402
from corpus import history as history_mod  # noqa: E402
from corpus import report as report_mod  # noqa: E402
from corpus import swebench_source, validate as validate_mod  # noqa: E402

POOL_PATH = REPO_ROOT / "benchmark" / "tasks" / "_candidates" / "django_pool.jsonl"
MANIFEST_PATH = REPO_ROOT / "benchmark" / "tasks" / "manifest.json"
REPORT_PATH = REPO_ROOT / "benchmark" / "task_selection_report.md"


def cmd_ingest(args) -> int:
    candidates = swebench_source.extract_candidates(
        repo=args.repo, limit=args.limit
    )
    path = swebench_source.write_candidate_pool(candidates, POOL_PATH)
    print(f"candidates: {len(candidates)}")
    print(f"pool: {path}")
    print(f"dataset: {cache.DATASET_REPO_ID}@{cache.DATASET_REVISION}")
    return 0


def cmd_validate(args) -> int:
    pool = swebench_source.read_candidate_pool(POOL_PATH)
    if args.limit:
        pool = pool[: args.limit]
    valid = 0
    for i, candidate in enumerate(pool, 1):
        cached = validate_mod.load_cached(REPO_ROOT, candidate["instance_id"])
        if cached and not args.force:
            if cached.get("valid"):
                valid += 1
            print(f"[{i}/{len(pool)}] {candidate['instance_id']} cached "
                  f"valid={cached.get('valid')}")
            continue
        result = validate_mod.validate_candidate(
            candidate, run_tests=not args.no_tests, allow_network=True
        )
        validate_mod.save_result(REPO_ROOT, result)
        if result["valid"]:
            valid += 1
        print(f"[{i}/{len(pool)}] {candidate['instance_id']} "
              f"valid={result['valid']} reason={result['failure_reason']}")
    print(f"validated: {valid}/{len(pool)}")
    return 0


def cmd_build(args) -> int:
    pool = swebench_source.read_candidate_pool(POOL_PATH)
    validation = {}
    gold_patches = {}
    for candidate in pool:
        result = validate_mod.load_cached(REPO_ROOT, candidate["instance_id"])
        if result:
            validation[candidate["instance_id"]] = result
    repo = cache.repo_clone_dir("django/django")

    # Derive real C/D setup payloads from the cached commit graph. A candidate
    # with no real git evidence simply does not qualify for C or D; nothing is
    # fabricated to fill a quota.
    memory_setups: dict[str, dict] = {}
    staleness_setups: dict[str, dict] = {}
    gold_files: dict[str, list[str]] = {}
    for candidate in pool:
        instance_id = candidate["instance_id"]
        if not validation.get(instance_id, {}).get("valid"):
            continue
        gold = swebench_source.load_patch(instance_id, "patch")
        gold_patches[instance_id] = gold
        files = manifest_mod.patch_shape(gold)["files"]
        gold_files[instance_id] = files
        if not repo.is_dir():
            continue
        episodic = history_mod.derive_episodic_setup(
            repo, candidate["base_commit"], files
        )
        if episodic:
            for event in episodic["events"]:
                event["repo"] = "django/django"
            memory_setups[instance_id] = episodic
        staleness = history_mod.derive_staleness_setup(
            repo, candidate["base_commit"], files
        )
        if staleness:
            for event in staleness["events"]:
                event["repo"] = "django/django"
            staleness_setups[instance_id] = staleness

    built = manifest_mod.build_manifest(
        pool,
        validation,
        gold_patches,
        memory_setups=memory_setups,
        staleness_setups=staleness_setups,
    )
    problems = manifest_mod.validate_manifest(built)
    manifest_mod.write_manifest(built, MANIFEST_PATH)

    # Deterministic selection report from the very data above.
    text = report_mod.build_report(
        pool=pool,
        validation=validation,
        manifest=built,
        selection_criteria=[
            "Dataset pinned by revision + parquet sha256; candidates taken in "
            "dataset order (no cherry-picking).",
            "A candidate enters only after real checkout, gold-patch apply, "
            "and FAIL_TO_PASS passage.",
            "Category assigned by gold-patch shape (A/B) and by real git "
            "evidence (C/D); quotas never force an unqualified task.",
            "Selection is independent of baseline/reference performance.",
        ],
        limitations=[
            "SWE-bench tasks are authored for Linux containers; host-OS "
            "validation can report environment_unsupported rather than a "
            "genuine pass/fail.",
            "Older Django lines (<4.1) are not runnable on the available "
            "Python 3.11 interpreter.",
            "C/D memory payloads are derived from real commits but are "
            "seeded, not replayed, so 'memory_used' must be proven by logs.",
        ],
    )
    report_mod.write_report(REPORT_PATH, text)

    print(f"manifest: {MANIFEST_PATH}")
    print(f"report: {REPORT_PATH}")
    print(f"tasks: {built['task_count']} distribution: {built['distribution']}")
    print(f"C setups: {len(memory_setups)} D setups: {len(staleness_setups)}")
    if problems:
        print(f"manifest problems: {problems}")
        return 1
    return 0


def cmd_probe(args) -> int:
    """Bounded, deterministic validation probe with an honest taxonomy.

    Selection rule (documented, deterministic, NOT outcome-based):
      candidates in dataset order, filtered to those the host interpreter can
      actually run (Django >= 4.1 on Python 3.11). No candidate is skipped
      because it looks likely to pass.

    Every candidate is recorded with its own timing and a taxonomy bucket so
    our environment is never conflated with task badness.
    """
    import time

    pool = swebench_source.read_candidate_pool(POOL_PATH)
    runnable = [
        c for c in pool
        if validate_mod._python_for_version(c.get("version")) is not None
    ]
    skipped_unsupported = len(pool) - len(runnable)
    selection = runnable[: args.limit]

    rows = []
    counts = {name: 0 for name in report_mod.TAXONOMY}
    counts["VALID"] = 0
    for i, candidate in enumerate(selection, 1):
        started = time.time()
        cached = validate_mod.load_cached(REPO_ROOT, candidate["instance_id"])
        if cached and not args.force:
            result = cached
        else:
            result = validate_mod.validate_candidate(
                candidate, run_tests=not args.no_tests, allow_network=True
            )
            validate_mod.save_result(REPO_ROOT, result)
        duration = result.get("duration_seconds")
        if duration is None:
            duration = round(time.time() - started, 2)

        if result.get("valid"):
            bucket = "VALID"
        else:
            bucket = report_mod.classify_failure(result.get("failure_reason"))
        counts[bucket] = counts.get(bucket, 0) + 1
        rows.append(
            {
                "candidate_id": candidate["instance_id"],
                "instance_id": candidate["instance_id"],
                "version": candidate.get("version"),
                "base_commit": candidate["base_commit"],
                "bucket": bucket,
                "classification": bucket,
                "valid": result.get("valid"),
                "checkout_result": "ok" if result.get("base_checkout") else "failed",
                "patch_result": "applied" if result.get("gold_patch_applies") else "failed",
                "test_patch_result": (
                    "applied" if result.get("test_patch_applies") else (
                        "missing" if not result.get("has_test_patch") else "failed"
                    )
                ),
                "tests_before_gold_patch": result.get("tests_fail_before_gold_patch"),
                "tests_after_gold_patch": result.get("tests_pass_after_gold_patch"),
                "FAIL_TO_PASS": result.get("f2p_labels") or [],
                "PASS_TO_PASS": result.get("p2p_labels") or [],
                "failure_reason": result.get("failure_reason"),
                "duration_seconds": duration,
            }
        )
        print(
            f"[{i}/{len(selection)}] {candidate['instance_id']} "
            f"v{candidate.get('version')} -> {bucket} "
            f"({duration}s) {result.get('failure_reason') or ''}"
        )

    out_path = REPO_ROOT / "benchmark" / "validation" / "_probe.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    out_path.write_text(
        _json.dumps(
            {
                "selection_rule": (
                    "dataset order, filtered to host-runnable Django (>=4.1); "
                    "no outcome-based skipping"
                ),
                "limit": args.limit,
                "pool": len(pool),
                "runnable_pool": len(runnable),
                "skipped_unsupported_environment": skipped_unsupported,
                "id_list_sha256": _json.loads(
                    (REPO_ROOT / "benchmark" / "validation"
                     / "_selection.json").read_text(encoding="utf-8")
                ).get("id_list_sha256"),
                "counts": counts,
                "candidates": rows,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print()
    print("=== PROBE SUMMARY ===")
    print(f"attempted: {len(selection)}")
    for name in report_mod.TAXONOMY:
        print(f"{name}: {counts.get(name, 0)}")
    print(f"pool={len(pool)} runnable_pool={len(runnable)} "
          f"skipped_unsupported={skipped_unsupported}")
    print(f"report: {out_path}")
    return 0


def cmd_status(args) -> int:
    pool = POOL_PATH.exists() and swebench_source.read_candidate_pool(POOL_PATH) or []
    results = sorted((cache.validation_dir(REPO_ROOT) / "candidates").glob("*.json"))
    valid = 0
    for path in results:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("valid"):
            valid += 1
    print(f"pool: {len(pool)}")
    print(f"validation files: {len(results)}")
    print(f"validated: {valid}")
    print(f"cache root: {cache.cache_root()}")
    print(f"manifest exists: {MANIFEST_PATH.exists()}")
    print(f"report exists: {REPORT_PATH.exists()}")
    return 0


def cmd_materialize(args) -> int:
    """Turn the manifest into runnable TRACE task dirs (adapter layer)."""
    if not MANIFEST_PATH.exists():
        print(f"no manifest at {MANIFEST_PATH}; run `build` first")
        return 1
    built = manifest_mod.load_manifest(MANIFEST_PATH)
    tasks_root = REPO_ROOT / "tasks"
    created = adapter_mod.materialize_manifest(
        built, tasks_root, force=args.force
    )
    print(f"materialized: {len(created)} task dirs under {tasks_root}")
    for path in created:
        print(f"  {path.relative_to(REPO_ROOT)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest")
    p_ingest.add_argument("--repo", default="django/django")
    p_ingest.add_argument("--limit", type=int, default=None)
    p_ingest.set_defaults(func=cmd_ingest)

    p_val = sub.add_parser("validate")
    p_val.add_argument("--limit", type=int, default=None)
    p_val.add_argument("--no-tests", action="store_true")
    p_val.add_argument("--force", action="store_true")
    p_val.set_defaults(func=cmd_validate)

    p_build = sub.add_parser("build")
    p_build.set_defaults(func=cmd_build)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=cmd_status)

    p_mat = sub.add_parser("materialize")
    p_mat.add_argument("--force", action="store_true")
    p_mat.set_defaults(func=cmd_materialize)

    p_probe = sub.add_parser("probe")
    p_probe.add_argument("--limit", type=int, default=20)
    p_probe.add_argument("--no-tests", action="store_true")
    p_probe.add_argument("--force", action="store_true")
    p_probe.set_defaults(func=cmd_probe)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
