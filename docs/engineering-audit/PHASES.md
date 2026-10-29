# Phase tracker

| Phase | Status | Gate evidence | Branch → push |
|---|---|---|---|
| Recovery (TASK 4/6) | **COMPLETE** | 28/28 WIP fidelity; scans clean; remote SHA verified | `recovery/trace-2026-10-29` @ `2b72a71` ✔ pushed |
| Phase 0 — reconcile/stabilize | **GATE PASSED** | fixtures reproducible; suites green pre/post adoption; A/B probe evidence; policy B adopted; DEF-2 classified; compatibility matrix | `phase/0-reliability` @ `309f58a` ✔ pushed; PR #1 open |
| Phase 1 — memory research | **EVIDENCE ADOPTED** (research artifacts + docs reconcile); human labels & paid-model arms still BLOCKED (B1/B2) | AI-assisted labels verified content-free; PROVENANCE.md reading rules; synthetic-material quality = suite + Phase-0 probes | `phase/1-research` (this branch) |
| Phase 2 — controlled evaluation | NOT STARTED (harness validated; prereg binding) | requires Phase 0 stable baseline | next branch |
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
