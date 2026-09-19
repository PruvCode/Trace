"""Selection report generation (``benchmark/task_selection_report.md``).

The report is the human-auditable record of *how* the corpus was chosen. It is
derived entirely from the pool + per-candidate validation results + the
manifest, so it can be regenerated deterministically and never drifts from the
data it describes.

Honesty requirements it must satisfy:
- state the dataset source and pinned revision;
- give candidate / validated / rejected counts with a rejection-reason
  breakdown (never a single opaque "rejected" number);
- separate *candidate-invalid* from *infrastructure/environment* failures —
  our environment must never be reported as task badness;
- list the final tasks with their category distribution;
- state the selection criteria and known limitations plainly.

Nothing here runs an agent or inspects baseline/reference performance.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

REPORT_VERSION = "2"

# Failure reasons that mean "our environment/harness could not evaluate it",
# NOT "the task is bad". Kept explicit so the report never conflates them.
INFRASTRUCTURE_REASONS = {
    "environment_unsupported",
    "repo_checkout_error",
    "checkout_failed",
    "checkout_timeout",
    "no_runtests_entrypoint",
    "no_runnable_test_labels",
    "test_run_timeout",
    "test_collection_error",
}

# Failure reasons that mean "the task itself cannot be validated", i.e. the
# dataset entry is not usable as a benchmark task. These are only assigned
# after patch, test_patch and the test control have actually been exercised.
CANDIDATE_INVALID_REASONS = {
    "no_fail_to_pass_tests",
    "missing_test_patch",
    "gold_patch_does_not_apply",
    "test_patch_does_not_apply",
    "base_commit_missing_from_repo",
    "tests_do_not_pass_after_gold_patch",
    "tests_pass_without_gold_patch",
    "tests_not_reproducible_before_gold_patch",
}

# The exact taxonomy the probe reports. Exposed so callers (and tests) can
# assert the yield accounting covers every bucket.
TAXONOMY = (
    "VALID",
    "CANDIDATE_INVALID",
    "CHECKOUT_FAILURE",
    "DEPENDENCY_FAILURE",
    "TEST_FAILURE",
    "TIMEOUT",
    "INFRASTRUCTURE_FAILURE",
)

_ENV_PREFIXES = ("environment_unsupported",)


def classify_failure(reason: str | None) -> str:
    """Bucket a validation failure_reason into the probe's taxonomy.

    Order matters: infrastructure, environment and dependency causes are
    checked before candidate-invalid so a harness problem can never be reported
    as a bad task. The returned strings are the taxonomy's own names, so the
    probe can aggregate directly without a second mapping.
    """
    if not reason:
        return "none"
    text = str(reason)
    if text.startswith(_ENV_PREFIXES):
        return "DEPENDENCY_FAILURE"
    if text.startswith("repo_checkout_error") or text.startswith("checkout_failed"):
        return "CHECKOUT_FAILURE"
    if text.startswith("checkout_timeout"):
        return "TIMEOUT"
    if "timeout" in text:
        return "TIMEOUT"
    if text in ("no_runtests_entrypoint", "no_runnable_test_labels"):
        return "INFRASTRUCTURE_FAILURE"
    if text in ("test_collection_error",):
        return "DEPENDENCY_FAILURE"
    if text == "tests_do_not_pass_after_gold_patch":
        return "TEST_FAILURE"
    if text in CANDIDATE_INVALID_REASONS:
        return "CANDIDATE_INVALID"
    return "INFRASTRUCTURE_FAILURE"


def _bucket_counts(validation: dict[str, dict]) -> Counter:
    counts: Counter = Counter()
    for result in validation.values():
        if result.get("valid"):
            counts["VALID"] += 1
        else:
            counts[classify_failure(result.get("failure_reason"))] += 1
    return counts


def rejection_breakdown(validation: dict[str, dict]) -> dict[str, int]:
    """Exact failure_reason -> count, for every non-valid result."""
    reasons: Counter = Counter()
    for result in validation.values():
        if result.get("valid"):
            continue
        reasons[str(result.get("failure_reason"))] += 1
    return dict(sorted(reasons.items()))


def build_report(
    *,
    pool: list[dict],
    validation: dict[str, dict],
    manifest: dict | None,
    classification_counts: dict[str, int] | None = None,
    selection_criteria: list[str] | None = None,
    limitations: list[str] | None = None,
) -> str:
    dataset = (pool[0]["dataset"] if pool else {}) or {}
    buckets = _bucket_counts(validation)
    reasons = rejection_breakdown(validation)
    validated = sum(1 for r in validation.values() if r.get("valid"))
    rejected = sum(1 for r in validation.values() if not r.get("valid"))
    infra = (
        buckets.get("INFRASTRUCTURE_FAILURE", 0)
        + buckets.get("CHECKOUT_FAILURE", 0)
    )
    timeout = buckets.get("TIMEOUT", 0)
    dependency = buckets.get("DEPENDENCY_FAILURE", 0)
    candidate_invalid = buckets.get("CANDIDATE_INVALID", 0)
    test_failure = buckets.get("TEST_FAILURE", 0)

    lines: list[str] = []
    lines.append("# TRACE Task Selection Report")
    lines.append("")
    lines.append(f"Report version: `{REPORT_VERSION}`")
    lines.append("")
    lines.append("## Dataset source / version")
    lines.append("")
    lines.append(f"- Dataset: `{dataset.get('id', 'unknown')}`")
    lines.append(f"- Revision: `{dataset.get('revision', 'unknown')}`")
    lines.append(f"- Split: `{dataset.get('split', 'unknown')}`")
    lines.append(f"- Parquet sha256: `{dataset.get('parquet_sha256', 'unknown')}`")
    lines.append("- Primary repository: `django/django`")
    lines.append("")
    lines.append("## Candidate pool")
    lines.append("")
    lines.append(f"- Candidates ingested: **{len(pool)}**")
    lines.append(f"- Candidates validated: **{len(validation)}**")
    lines.append(f"- Valid: **{validated}**")
    lines.append(f"- Rejected: **{rejected}**")
    lines.append("")
    lines.append("### Rejection taxonomy (honest separation)")
    lines.append("")
    lines.append("| Bucket | Count | Meaning |")
    lines.append("| --- | --- | --- |")
    lines.append(
        f"| CANDIDATE_INVALID | {candidate_invalid} | task genuinely broken/ambiguous |"
    )
    lines.append(
        f"| CHECKOUT_FAILURE | {infra} | our harness could not check out the base commit |"
    )
    lines.append(
        f"| DEPENDENCY_FAILURE | {dependency} | environment/dependency could not run the tests |"
    )
    lines.append(
        f"| TEST_FAILURE | {test_failure} | tests ran but did not pass after the gold patch |"
    )
    lines.append(f"| TIMEOUT | {timeout} | evaluation exceeded its time budget |")
    lines.append("")
    if reasons:
        lines.append("### Exact rejection reasons")
        lines.append("")
        lines.append("| failure_reason | count |")
        lines.append("| --- | --- |")
        for reason, count in reasons.items():
            lines.append(f"| `{reason}` | {count} |")
        lines.append("")

    lines.append("## Final corpus")
    lines.append("")
    if manifest:
        dist = manifest.get("distribution") or {}
        lines.append(f"- Tasks: **{manifest.get('task_count', 0)}**")
        lines.append(f"- Target distribution: `{manifest.get('target_distribution')}`")
        lines.append(f"- Actual distribution: `{dist}`")
        lines.append("")
        lines.append("| task_id | category | repo | base_commit | validation |")
        lines.append("| --- | --- | --- | --- | --- |")
        for task in manifest.get("tasks", []):
            v = task.get("validation_status", {}) or {}
            lines.append(
                f"| `{task['task_id']}` | {task['category']} | {task['repo']} | "
                f"`{str(task['base_commit'])[:12]}` | valid={v.get('valid')} |"
            )
        lines.append("")
    else:
        lines.append("_No manifest yet._")
        lines.append("")

    if classification_counts:
        lines.append("## Classification of the validated pool")
        lines.append("")
        lines.append("| category | count |")
        lines.append("| --- | --- |")
        for category in ("A_control", "B_structural", "C_episodic", "D_staleness"):
            lines.append(f"| {category} | {classification_counts.get(category, 0)} |")
        lines.append("")

    lines.append("## Selection criteria")
    lines.append("")
    for item in selection_criteria or []:
        lines.append(f"- {item}")
    lines.append("")
    lines.append(
        "Selection is performed **before** any baseline/reference measurement "
        "exists; no task is added or removed based on experimental outcomes."
    )
    lines.append("")

    lines.append("## Evaluator validation")
    lines.append("")
    lines.append(
        "Every task carries an objective evaluator: the repository's own test "
        "runner over the SWE-bench FAIL_TO_PASS specification. A task enters "
        "the corpus only if the gold patch applies and those tests pass after "
        "it (and, where measured, fail before it)."
    )
    lines.append("")

    lines.append("## Known limitations")
    lines.append("")
    for item in limitations or []:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def write_report(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path
