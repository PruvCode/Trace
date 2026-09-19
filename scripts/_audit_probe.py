"""Audit helper for the deterministic 20-candidate probe (Step FIVE).

Reads benchmark/validation/_probe.json and answers, per candidate and in
aggregate:

- what the taxonomy bucket is;
- whether TRACE's own harness caused the outcome (harness-caused failures are
  reported separately and NEVER silently folded into "the candidate is bad");
- the exact selection rule and the probe's identity (sha256 of the id list) so
  the audit cannot be confused with a differently-selected run.

Deliberately read-only: it prints a table and aggregates. It writes nothing.
Run:  python scripts/_audit_probe.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "benchmark" / "validation" / "_probe.json"

# Buckets that indicate the harness/environment, not the candidate. A failure
# here means the candidate was never fairly evaluated.
HARNESS_BUCKETS = {
    "INFRASTRUCTURE_FAILURE",
    "CHECKOUT_FAILURE",
    "TIMEOUT",
    "DEPENDENCY_FAILURE",
}

DETAIL_FIELDS = (
    "candidate_id",
    "version",
    "base_commit",
    "bucket",
    "checkout_result",
    "patch_result",
    "test_patch_result",
    "tests_before_gold_patch",
    "tests_after_gold_patch",
    "duration_seconds",
)


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "..."


def main() -> int:
    if not PROBE.exists():
        print(f"probe output not found: {PROBE}")
        print("run: python scripts/corpus.py probe --limit 20 --force")
        return 2

    data = json.loads(PROBE.read_text(encoding="utf-8"))
    candidates = data.get("candidates", [])
    counts = Counter(row.get("bucket") for row in candidates)

    print("=" * 100)
    print("TRACE corpus probe audit")
    print("=" * 100)
    print(f"selection_rule : {data.get('selection_rule')}")
    print(f"id_list_sha256 : {data.get('id_list_sha256')}")
    print(f"pool           : {data.get('pool')}")
    print(f"runnable_pool  : {data.get('runnable_pool')}")
    print(f"skipped_env    : {data.get('skipped_unsupported_environment')}")
    print(f"limit          : {data.get('limit')}")
    print(f"audited        : {len(candidates)}")
    print()

    header = ("candidate_id", "v", "patch", "tpatch", "pre", "post", "secs", "bucket")
    widths = (26, 5, 8, 8, 5, 5, 8, 22)
    print("".join(str(h).ljust(w) for h, w in zip(header, widths)))
    print("-" * sum(widths))
    for row in candidates:
        line = (
            _fmt(row.get("candidate_id")).ljust(widths[0])
            + _fmt(row.get("version")).ljust(widths[1])
            + _fmt(row.get("patch_result")).ljust(widths[2])
            + _fmt(row.get("test_patch_result")).ljust(widths[3])
            + _fmt(row.get("tests_before_gold_patch")).ljust(widths[4])
            + _fmt(row.get("tests_after_gold_patch")).ljust(widths[5])
            + _fmt(row.get("duration_seconds")).ljust(widths[6])
            + _fmt(row.get("bucket")).ljust(widths[7])
        )
        print(line)
        if row.get("failure_reason"):
            print(f"    reason: {row['failure_reason']}")
    print()

    print("-" * 100)
    print("Aggregate")
    print("-" * 100)
    valid = counts.get("VALID", 0)
    total = len(candidates)
    for name in sorted(counts):
        print(f"  {name:<26} {counts[name]}")
    print()
    yield_pct = (valid / total * 100) if total else 0.0
    print(f"  yield: {valid}/{total} = {yield_pct:.1f}%")

    harness = sum(counts.get(b, 0) for b in HARNESS_BUCKETS)
    if harness:
        print()
        print(f"  WARNING: {harness} candidate(s) failed in a bucket that indicates")
        print("  the harness/environment rather than the candidate. These are NOT")
        print("  evidence about the candidate and must be investigated before the")
        print("  probe can be called trustworthy.")
        for row in candidates:
            if row.get("bucket") in HARNESS_BUCKETS:
                print(
                    f"    - {row.get('candidate_id')}: {row.get('bucket')} "
                    f"({row.get('failure_reason')})"
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
