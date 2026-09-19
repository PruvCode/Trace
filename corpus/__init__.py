"""SWE-bench ingestion + validation for the TRACE 40-task corpus.

This package is deliberately separate from the benchmark *engine*
(``benchmark.runner``): it produces the task manifest the engine consumes.
It never runs an agent and never evaluates a run.

Pipeline:
    dataset (pinned) -> candidates (pool) -> validated pool -> classified
    manifest (40 tasks) -> TRACE runner consumes the manifest.

Design rules honoured here:
- deterministic ingestion (pinned dataset revision + parquet sha256),
- no blind trust in dataset metadata (real checkout + gold patch + tests),
- validation results cached so repositories are not re-downloaded,
- candidate pool is built BEFORE any baseline/reference performance exists.
"""

from __future__ import annotations

__all__ = [
    "cache",
    "swebench_source",
    "validate",
    "manifest",
    "history",
    "adapter",
    "report",
]
