# Phase tracker

| Phase | Status | Gate evidence | Branch → push |
|---|---|---|---|
| Recovery (TASK 4/6) | **COMPLETE** | 28/28 WIP fidelity; scans clean; remote SHA verified | `recovery/trace-2026-10-29` @ `2b72a71` ✔ pushed |
| Phase 0 — reconcile/stabilize | **IN PROGRESS** | fixtures reproducible (94 benchmark tests, then 438/29/1 full suite); A/B injection probe running | `phase/0-reliability` @ `6aace71` ✔ pushed (not final) |
| Phase 1 — memory research | BLOCKED (human labels) / partial | selection-bias analysis verified; capture-audit derived with documented Permission-D caveat; pilot review AI-assisted only | not pushed yet |
| Phase 2 — controlled evaluation | NOT STARTED (harness validated; prereg exists) | requires Phase 0 stable baseline | — |
| Phase 3 — justified improvements | NOT STARTED | gated on Phase 1/2 evidence | — |
| Phase 4 — hardening/release | NOT STARTED | incl. OpenCode 2.x + docs accuracy + final audit | — |

## Phase 0 acceptance checklist (§5.4)

- [x] Reconciled code passes deterministic tests on a clean checkout (438 passed / 29 skipped / 1 xfailed).
- [x] Fixture setup reproducible from a clean checkout (deterministic seeders, pins verified).
- [x] Root-caused `test_analyze` failure (fixture absence → empty category mapping), no test weakening.
- [ ] Justified injection policy (A vs B mean-measured probe — running).
- [ ] Honest compatibility matrix (OpenCode 1.18.32 disposable OK; 2.1.4 NOT VALIDATED).
- [ ] Phase ledger updated; branch pushed; PR opened; merge only after full gate.
