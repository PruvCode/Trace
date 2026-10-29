# Phase tracker

| Phase | Status | Gate evidence | Branch → push |
|---|---|---|---|
| Recovery (TASK 4/6) | **COMPLETE** | 28/28 WIP fidelity; scans clean; remote SHA verified | `recovery/trace-2026-10-29` @ `2b72a71` ✔ pushed |
| Phase 0 — reconcile/stabilize | **GATE PASSED (pending final commit/PR)** | fixtures reproducible; clean-checkout suites green pre- and post-adoption; A/B probe evidence recorded; policy B adopted | `phase/0-reliability` (adoption commit follows) |
| Phase 1 — memory research | BLOCKED (human labels) / partial | selection-bias analysis verified; capture-audit derived with documented Permission-D caveat; pilot review AI-assisted only | not pushed yet |
| Phase 2 — controlled evaluation | NOT STARTED (harness validated; prereg exists) | requires Phase 0 stable baseline | — |
| Phase 3 — justified improvements | NOT STARTED | gated on Phase 1/2 evidence | — |
| Phase 4 — hardening/release | NOT STARTED | incl. OpenCode 2.x + docs accuracy + final audit | — |

## Phase 0 acceptance checklist (§5.4)

- [x] Reconciled code passes deterministic tests on a clean checkout (438/29/1 pre-adoption; 439/29/1 post-adoption; zero fail/err both).
- [x] Fixture setup reproducible from a clean checkout (deterministic seeders, pins verified).
- [x] Root-caused `test_analyze` failure (fixture absence → empty category mapping), no test weakening.
- [x] Justified injection policy: per-request adopted on probe evidence (A spread 1/9+1/8, B 5/5, non-persistence verified both, no dups, audit of Claude's tests = no weakening); semantic recall recorded as INCONCLUSIVE.
- [x] Honest compatibility matrix (OpenCode 1.18.32 disposable E2E validated this session; 2.1.4 NOT VALIDATED — recorded).
- [x] DEF-2 classified (model-free repro): `monitoring:error` = documented "OpenCode DB not found" precondition (probe lacked the harness primer; self-healed via plugin ensure-capture); flush `busy` = capture_once single-capture guard. No product fix; Phase-3 candidate status (`not_ready`) recorded.
- [ ] Phase ledger updated with final SHAs; branch pushed; PR opened; merge only after full gate review.
