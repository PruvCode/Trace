"""Final manifest: classified 40-task corpus + selection report.

Classification is rule-based and deterministic, derived from the gold patch
shape and the problem statement — never from experiment results. The corpus is
selected BEFORE any baseline/reference run exists (experimental-integrity rule
in the task brief).

Categories:
  A_control    self-contained, single-file, small diff, few tests.
  B_structural cross-file / relationship-heavy changes.
  C_episodic   task where a real, provenance-bearing prior investigation is
               derivable from the repository's own git history.
  D_staleness  task where a real earlier fact was contradicted by a later
               commit before base_commit, so seeded memory is genuinely stale.

C and D are not fabricated: their memory payloads come from real git commits
in the cached clone. A candidate only qualifies when such a commit exists.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from corpus import cache

MANIFEST_VERSION = "1"

TARGET_DISTRIBUTION = {
    "A_control": 8,
    "B_structural": 12,
    "C_episodic": 12,
    "D_staleness": 8,
}

_DIFF_FILE_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.M)
_HUNK_RE = re.compile(r"^[+-](?![+-])", re.M)


def patch_shape(gold_patch: str) -> dict:
    """Derive file count and changed-line count from a unified diff."""
    files = sorted({m.group(2) for m in _DIFF_FILE_RE.finditer(gold_patch)})
    changed_lines = len(_HUNK_RE.findall(gold_patch))
    return {"files": files, "file_count": len(files), "changed_lines": changed_lines}


def classify(candidate: dict, gold_patch: str, *, has_prior_history: bool = False,
             has_stale_predecessor: bool = False) -> str:
    """Deterministic category assignment for one candidate.

    Order matters: staleness and episodic qualification require real git
    evidence and take precedence only when that evidence exists; otherwise the
    task falls back to structural/control purely on patch shape.
    """
    shape = patch_shape(gold_patch)
    f2p = len((candidate.get("test_spec") or {}).get("FAIL_TO_PASS") or [])

    if has_stale_predecessor:
        return "D_staleness"
    if has_prior_history:
        return "C_episodic"
    if shape["file_count"] >= 2:
        return "B_structural"
    # A_control: single-file, small, few tests. Anything larger and single-file
    # is treated as structural rather than forced into control.
    if shape["file_count"] == 1 and shape["changed_lines"] <= 15 and f2p <= 3:
        return "A_control"
    return "B_structural"


def build_manifest(
    candidates: list[dict],
    validation: dict[str, dict],
    gold_patches: dict[str, str],
    *,
    memory_setups: dict[str, dict] | None = None,
    staleness_setups: dict[str, dict] | None = None,
    selection_reasons: dict[str, str] | None = None,
    quotas: dict[str, int] | None = None,
) -> dict:
    """Assemble the manifest from validated candidates.

    ``candidates`` must already be restricted to the validated pool; the caller
    decides ordering so selection is reproducible.
    """
    memory_setups = memory_setups or {}
    staleness_setups = staleness_setups or {}
    selection_reasons = selection_reasons or {}
    quotas = quotas or TARGET_DISTRIBUTION

    buckets: dict[str, list[dict]] = {k: [] for k in TARGET_DISTRIBUTION}
    for candidate in candidates:
        instance_id = candidate["instance_id"]
        result = validation.get(instance_id)
        if not result or not result.get("valid"):
            continue
        gold = gold_patches.get(instance_id, "")
        category = classify(
            candidate,
            gold,
            has_prior_history=instance_id in memory_setups,
            has_stale_predecessor=instance_id in staleness_setups,
        )
        buckets[category].append(candidate)

    tasks = []
    for category, quota in quotas.items():
        for candidate in buckets[category][:quota]:
            instance_id = candidate["instance_id"]
            result = validation[instance_id]
            tasks.append(
                {
                    "task_id": instance_id,
                    "source": candidate["source"],
                    "source_task_id": candidate["source_task_id"],
                    "repo": candidate["repo"],
                    "base_commit": candidate["base_commit"],
                    "category": category,
                    "problem_statement": candidate["problem_statement"],
                    "evaluator": {
                        "kind": "swebench_test_spec",
                        "FAIL_TO_PASS": (candidate.get("test_spec") or {}).get(
                            "FAIL_TO_PASS", []
                        ),
                        "PASS_TO_PASS": (candidate.get("test_spec") or {}).get(
                            "PASS_TO_PASS", []
                        ),
                        "runner": "tests/runtests.py",
                    },
                    "validation_status": {
                        "valid": result.get("valid"),
                        "base_checkout": result.get("base_checkout"),
                        "base_commit_exists": result.get("base_commit_exists"),
                        "tests_available": result.get("tests_available"),
                        "gold_patch_applies": result.get("gold_patch_applies"),
                        "tests_pass_after_gold_patch": result.get(
                            "tests_pass_after_gold_patch"
                        ),
                        "failure_reason": result.get("failure_reason"),
                    },
                    "selection_reason": selection_reasons.get(
                        instance_id, f"classified {category} by patch shape"
                    ),
                    "memory_setup": memory_setups.get(instance_id),
                    "staleness_setup": staleness_setups.get(instance_id),
                    "dataset": candidate["dataset"],
                    "version": candidate.get("version"),
                }
            )

    distribution = {k: sum(1 for t in tasks if t["category"] == k) for k in quotas}
    return {
        "manifest_version": MANIFEST_VERSION,
        "dataset": {
            "id": cache.DATASET_REPO_ID,
            "revision": cache.DATASET_REVISION,
            "split": cache.DATASET_SPLIT_NAME,
            "parquet_sha256": cache.DATASET_PARQUET_SHA256,
        },
        "target_distribution": quotas,
        "distribution": distribution,
        "task_count": len(tasks),
        "tasks": tasks,
    }


def validate_manifest(manifest: dict) -> list[str]:
    """Return a list of structural problems ([] when the manifest is sound)."""
    problems: list[str] = []
    seen: set[str] = set()
    for task in manifest.get("tasks", []):
        for field in (
            "task_id",
            "source",
            "source_task_id",
            "repo",
            "base_commit",
            "category",
            "problem_statement",
            "evaluator",
            "validation_status",
            "selection_reason",
        ):
            if field not in task:
                problems.append(f"{task.get('task_id')}: missing {field}")
        if task["task_id"] in seen:
            problems.append(f"duplicate task_id: {task['task_id']}")
        seen.add(task["task_id"])
        if task["category"] not in TARGET_DISTRIBUTION:
            problems.append(f"{task['task_id']}: bad category {task['category']}")
    return problems


def write_manifest(manifest: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
