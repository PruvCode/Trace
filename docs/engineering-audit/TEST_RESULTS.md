# Test results (engineering ledger)

## Phase 0 — clean-checkout baseline (`phase/0-reliability`)

| # | Suite / experiment | Command | Result | Commit |
|---|---|---|---|---|
| T1 | tests/benchmark, clean checkout, before fixture fix | `python -m pytest tests/benchmark -q` | **32 failed, 50 passed, 12 errors** (69.9s) — reproduced on `2b72a71` tree | pre-fix |
| T2 | test_analyze focused, before fix | `pytest tests/benchmark/test_analyze.py -q` | **1 failed (task_categories_mapping), 8 passed** (1.27s) | pre-fix |
| T3 | determinism of seeded toy fixture | `python scripts/seed_toy_repo.py` vs pre-existing fixture SHA | identical `db386fe0dfdacf4b96877defef1395f2ea5425ce` | `246eea2` |
| T4 | history fixture determinism, clean seed | `python scripts/seed_history_repo.py` (clean dir) | 3 SHAs `8b30e539`, `43068f5b`, `a97390a8`; benchmark pins match `a97390a8` | `6aace71` |
| T5 | test_git_history + test_tasks_c + test_tasks_d after fix | focused pytest | **38 passed** (69.8s); was 17 failed + 11 errors | `6aace71` |
| T6 | FULL suite, clean checkout, baseline WIP | `pytest tests -q` | **438 passed, 29 skipped, 1 xfailed** in 197.3s (first run had env stragglers: re-run `268.7s` same counts; wait — see note) | pre-adoption |
| T7 | affected tests after adoption patch | `pytest tests/memory/test_session_context.py tests/memory/test_plugin_helper_retry.py tests/memory/test_long_session_reliability.py -q` | **51 passed, 1 xfailed** (33.1s) | pre-commit adoption |
| T8 | FULL suite, clean checkout, adopted | `pytest tests -q` | **439 passed, 29 skipped, 1 xfailed, 0 failed/errored** in 504.6s | adoption commit |

Note on T6: the exact numbers are `438 passed, 29 skipped, 1 xfailed` in **268.71s** (logged
as `full_suite_phase0_baseline.log`); an initial failed run of the same suite (before the
history-fixture fix) had measured the broken state, not this one.

### Skips (T8 context, 468 collected)
Live-OpenCode blackbox suites and node-dependent tests registered by
`tests/conftest.py` are opt-in (`TRACE_LIVE_OPENCODE=1`) and therefore skipped by design in
the deterministic suite; remaining skips are environment-conditional (node availability,
swebench external cache junction), auto-reported by pytest skip reasons.

## Injection-policy probes (`research/phase0/`, disposable OpenCode 1.18.32, free model)

Model: `opencode/big-pickle`. Task: `C_episodic/task_02_decision` (v2 token-format fact
seeded by session A via a real run + TRACE capture; session B must run a tool call and then
write `recall_answer.md` from memory on a later request). Task-awareness disabled
(`TRACE_TASK_AWARE=0`) in both arms. Results (2 trials per policy; trial B.0 skipped:
session-A monitor error → no store facts):

| Measure | Policy A (once-per-session) | Policy B (every-request) |
|---|---|---|
| injection spread | **1/9** requests (trial 0), **1/8** (trial 1) | **5/5** requests (trial 1) |
| injected chars | 549 / 439 | 484 |
| block present in OpenCode store (`message`/`part` data) | **false** | **false** |
| duplicate blocks on a request already carrying one | 0 | 0 |
| recall artifact written | ✗ (0/2) | ✗ (0/1) |
| input/output tokens | 16,960/998 + 16,853/760 | 13,579/541 |

Verified conclusions (mechanism-level):
1. **D3 premise independently confirmed**: OpenCode never persists the injected synthetic
   block; the block is NOT in the OpenCode message store under either policy.
2. **Policy A cannot supply memory on later requests** — exactly once injection with
   non-persistence = memory lost from request 2 onward (observed 1/9, 1/8).
3. **Policy B injects on every request with dedup** — observed 5/5 injections, no duplicates.
4. **Semantic recall verdict: INCONCLUSIVE** — the free model never wrote the required
   answer file under either policy even when the block carried the fact (reconstruction of
   policy-A trial-0 block shows the v2 rule inside the block). Recorded honestly: no
   product improvement claim may rest on this probe.
5. Structural adoption decision: per-request injection is the only mechanism consistent
   with non-persistence; overhead is bounded (`MAX_CHARS`) and helper caching is retained
   (`spawns == 2` across sessions), as asserted by the updated plugin tests.

Probe JSONL evidence: `research/phase0/results/probe_policy{A,B}_trial.jsonl`.

## DEF-2 focused repro (model-free, `%TEMP%\trace_recon\def2_repro.py`)

| Step | Expected | Observed |
|---|---|---|
| setup_integration, fresh home, no OpenCode DB | precondition error, verbatim message | `{"monitoring": {"status": "error", "message": "OpenCode database not found at <sandbox>\data\opencode\opencode.db. Run OpenCode at least once first."}}` |
| `opencode models` primer (disposable 1.18.32) | rc 0 and DB created | rc 0; `db exists now: True` |
| setup_integration again | service starts | `{"monitoring": {"status": "started", "pid": 5364}}` |
| `capture_once` while monitor live | busy guard | `{"status": "busy", "message": "a monitor is already capturing"}`, `living: true` |
| `stop()` | clean stop | `{"status": "stopped"}` |
| `capture_once` after stop | idle catch-up ok | `{"status": "ok", "captured": 0}`, `living: false` |

Verdict: both probe-era DEF-2 symptoms are designed behavior (precondition fail-fast +
single-capture guard); no product fix. Phase-3 candidate recorded: distinct
`not_ready` status instead of generic `error`.

## Current-state audit, 2026-10-30

See `CURRENT_STATE_AUDIT.md` section 4. Fresh default Windows clone at `2489795`:
**439 passed, 0 failed, 29 skipped, 1 xfailed** in 601 s; `tests/corpus` with `pyarrow`: 80 passed.
T6 and T8 above do not reproduce on a default clone (28 failed before the fix). The 21 live tests
were not run: the provider returned `Free usage limit reached`.
