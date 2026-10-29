# Test evidence

## Baseline manifest (original tree, 2026-10-28)
- branch clean-history, HEAD eb467f89efc0b982e0af3e1c9205dcecc1d8b931
- 23 modified tracked, 5 untracked, 0 staged; `git diff | git hash-object --stdin` = 5fec0f69b266f814ab56cc421043eec0d9fffedf
- untracked sha256:
  - 611175841476892c… tests/memory/test_long_session_reliability.py
  - bc9e9014b2973f84… tests/memory/test_memory_lifecycle.py
  - 7fd9dbccf48482a0… tests/memory/test_opencode_isolation.py
  - ceb0182f38efe982… tests/memory/test_plugin_helper_retry.py
  - b8d43bba13bac756… tests/memory/test_service_liveness.py
- worktree after import: identical diff hash, all 5 sha256 OK

## Suite runs (worktree)
| Run | Result | Notes |
|---|---|---|
| R1 (`-x`) | 1 failed, 8 passed, wall 9h48m | failure = DEF-1/D4 (fixtures absent). Wall time: the same 9 tests take 9 s; the machine was very likely asleep. Not counted as a hang without reproduction. |
| R2 (full, fixtures copied without their seeded `.git`) | 19 failed, 408 passed, 29 skipped, 1 xfailed, 11 errors (3h46m) | all failures but one = `fixture HEAD a374b9b != base_commit a97390a8`: re-seeding produced different SHAs (setup artefact, D2). Remaining one = my over-strict new assertion. |
| R3 (the 11 affected files, fixtures copied with `.git`) | 133 passed, 1 xfailed, 1 failed → fixed → plugin file 6/6 | `test_diagnostic_line_reports_outcome`: `items` is only logged on the spawning request; assertion corrected. |
| R4 (session/message context + new task-aware test) | 39 passed | |
