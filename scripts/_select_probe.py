"""Deterministic selection of exactly 20 probe candidates from the runnable pool.

SELECTION RULE (fixed BEFORE observing any validation outcome):

  1. Read benchmark/tasks/_candidates/django_pool.jsonl (850 Django candidates,
     in SWE-bench dataset order).
  2. Keep only candidates whose `version` maps to a host-available interpreter
     (validate._python_for_version(version) is not None). This is the "runnable"
     filter and has NOTHING to do with expected success.
  3. Keep the first 20 in dataset order.

No step inspects: expected success, memory usefulness, patch size, test count,
FAIL_TO_PASS volume, or ease of solving. The only filter is host-runnability,
which is an ENVIRONMENT capability, not an outcome signal.

This script prints the selection with a sha256 of the resulting id list so the
selection is auditable and reproducible.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from corpus import validate

TRACE = Path(__file__).resolve().parents[1]
POOL = TRACE / "benchmark" / "tasks" / "_candidates" / "django_pool.jsonl"
LIMIT = 20


def main() -> int:
    rows = []
    with POOL.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    runnable, skipped = [], []
    for r in rows:
        if validate._python_for_version(r.get("version")) is not None:
            runnable.append(r)
        else:
            skipped.append(r)

    selected = runnable[:LIMIT]
    ids = [r["instance_id"] for r in selected]
    digest = hashlib.sha256("\n".join(ids).encode()).hexdigest()

    print(f"pool_total            = {len(rows)}")
    print(f"runnable (host py)    = {len(runnable)}")
    print(f"skipped (no interp)   = {len(skipped)}")
    print(f"selected              = {len(selected)}")
    print(f"id_list_sha256        = {digest}")
    print()
    print("selection rule: dataset order, filtered to host-runnable Django; first 20; no outcome-based skipping")
    print()
    for i, r in enumerate(selected, 1):
        print(f"  {i:2d}. {r['instance_id']:28s} v{r.get('version'):6s} {r['base_commit'][:12]}")

    out = {
        "selection_rule": "dataset order, filtered to host-runnable Django (interpreter available for version); first 20; no outcome-based skipping",
        "pool_total": len(rows),
        "runnable_pool": len(runnable),
        "skipped_unsupported_environment": len(skipped),
        "limit": LIMIT,
        "id_list_sha256": digest,
        "selected": ids,
    }
    dest = TRACE / "benchmark" / "validation" / "_selection.json"
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
