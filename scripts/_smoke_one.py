"""Smoke-test ONE candidate end-to-end with the fixed validator.

Run: .venv/Scripts/python.exe scripts/_smoke_one.py
Prints the full result dict so every stage is auditable.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from corpus import swebench_source, validate  # noqa: E402

POOL = REPO_ROOT / "benchmark" / "tasks" / "_candidates" / "django_pool.jsonl"


def main() -> int:
    rows = swebench_source.read_candidate_pool(POOL)
    by_id = {r["instance_id"]: r for r in rows}
    target = sys.argv[1] if len(sys.argv) > 1 else "django__django-11692"
    candidate = by_id[target]

    print(f"candidate : {target}")
    print(f"version   : {candidate.get('version')}")
    print(f"base      : {candidate['base_commit']}")
    print(f"F2P       : {candidate['test_spec']['FAIL_TO_PASS']}")
    print("-" * 70, flush=True)

    started = time.time()
    result = validate.validate_candidate(candidate, run_tests=True)
    elapsed = round(time.time() - started, 1)

    print(json.dumps(result, indent=2, sort_keys=True))
    print("-" * 70)
    print(f"wall_clock_seconds={elapsed}")
    print(f"valid={result['valid']} classification={result['classification']}")
    print(f"failure_reason={result['failure_reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
