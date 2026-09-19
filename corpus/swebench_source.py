"""Deterministic SWE-bench ingestion (dataset -> candidate pool).

Pinned to one dataset revision so the same corpus can be regenerated exactly.
Only ``django/django`` instances are extracted (the task's stated primary
source). Full patch bodies are NOT copied into the pool: they stay in the
cached parquet and are referenced by instance id + sha256, so the pool never
duplicates the multi-MB upstream payload.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

from corpus import cache

INGEST_VERSION = "1"

# Fields preserved on every candidate (the task's "each candidate should
# preserve" list). ``gold_patch``/``test_patch`` are kept as references.
_CANDIDATE_FIELDS = (
    "instance_id",
    "repo",
    "base_commit",
    "problem_statement",
    "created_at",
    "version",
    "environment_setup_commit",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_dataset(force: bool = False) -> Path:
    """Download the pinned parquet if needed; verify its sha256.

    Raises RuntimeError on a checksum mismatch: a silently different dataset
    would invalidate every downstream corpus.
    """
    path = cache.dataset_parquet_path()
    if force or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.part")
        with urllib.request.urlopen(cache.DATASET_URL, timeout=300) as resp:
            tmp.write_bytes(resp.read())
        tmp.replace(path)
    actual = _sha256_file(path)
    if actual != cache.DATASET_PARQUET_SHA256:
        raise RuntimeError(
            "dataset parquet sha256 mismatch: "
            f"expected {cache.DATASET_PARQUET_SHA256}, got {actual}"
        )
    return path


def _read_rows(parquet_path: Path) -> list[dict]:
    import pyarrow.parquet as pq  # local import: optional ingest dependency

    return pq.read_table(parquet_path).to_pylist()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_candidates(
    repo: str = "django/django",
    *,
    limit: int | None = None,
    ensure: bool = True,
) -> list[dict]:
    """Return candidate dicts for ``repo`` from the pinned dataset.

    Deterministic: rows are returned in dataset order and filtered by repo.
    """
    parquet_path = cache.dataset_parquet_path()
    if ensure or not parquet_path.exists():
        parquet_path = ensure_dataset()
    rows = _read_rows(parquet_path)

    candidates: list[dict] = []
    for row in rows:
        if row.get("repo") != repo:
            continue
        gold = row.get("patch") or ""
        test_patch = row.get("test_patch") or ""
        candidate = {
            "source": "swe-bench",
            "source_task_id": row["instance_id"],
            "instance_id": row["instance_id"],
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "problem_statement": row["problem_statement"],
            "created_at": row.get("created_at"),
            "version": row.get("version"),
            "environment_setup_commit": row.get("environment_setup_commit"),
            "test_spec": {
                "FAIL_TO_PASS": json.loads(row.get("FAIL_TO_PASS") or "[]"),
                "PASS_TO_PASS": json.loads(row.get("PASS_TO_PASS") or "[]"),
            },
            # References, not payloads (avoid duplicating upstream data):
            "gold_patch_sha256": _sha256_text(gold),
            "test_patch_sha256": _sha256_text(test_patch),
            "gold_patch_ref": {
                "dataset": cache.DATASET_REPO_ID,
                "revision": cache.DATASET_REVISION,
                "split": cache.DATASET_SPLIT_NAME,
                "field": "patch",
            },
            "test_patch_ref": {
                "dataset": cache.DATASET_REPO_ID,
                "revision": cache.DATASET_REVISION,
                "split": cache.DATASET_SPLIT_NAME,
                "field": "test_patch",
            },
            "dataset": {
                "id": cache.DATASET_REPO_ID,
                "revision": cache.DATASET_REVISION,
                "split": cache.DATASET_SPLIT_NAME,
                "parquet_sha256": cache.DATASET_PARQUET_SHA256,
            },
        }
        candidates.append(candidate)
        if limit is not None and len(candidates) >= limit:
            break
    return candidates


def load_patch(instance_id: str, field: str, repo: str = "django/django") -> str:
    """Fetch a full patch body from the cached parquet by instance id.

    Keeps the pool small while still making gold/test patches available to the
    validator on demand.
    """
    if field not in ("patch", "test_patch"):
        raise ValueError(f"unsupported patch field: {field!r}")
    for row in _read_rows(cache.dataset_parquet_path()):
        if row.get("repo") == repo and row.get("instance_id") == instance_id:
            return row.get(field) or ""
    raise KeyError(f"instance not found in dataset: {instance_id}")


def write_candidate_pool(
    candidates: list[dict], out_path: Path
) -> Path:
    """Write the pool as JSONL (one candidate per line, sorted keys)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for candidate in candidates:
            fh.write(json.dumps(candidate, sort_keys=True) + "\n")
    return out_path


def read_candidate_pool(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows
