# Engineering status — GLM-5.3 takeover

Workspace: `C:\Users\pruth\trace-glm` (independent clone of GitHub `PruvCode/Trace`, outside OneDrive).
This ledger supersedes, for recovered engineering work, the autonomous-workspace ledger
(`docs/autopilot/` in Claude's workspace); research evidence there is audited and either
re-verified or labelled, never re-executed without need.

## Three-way recovery (done 2026-10-29)

| Source | State at takeover |
|---|---|
| A — original OneDrive tree | branch `clean-history` @ `eb467f89…`, 23 modified + 0 staged + 5 untracked; tracked-diff hash `5fec0f69…`, SHA-256 `54a179b9…` reproduce exactly. Untouched. |
| B — Claude workspace (` trace-autonomous`) | branch `autonomous/trace-research` @ `4363ff42`, 4 commits (d825c8c WIP import, baedd4b autopilot, ea316f2 prereg, 4363ff4 harness fix), 4 uncommitted doc edits + 3 untracked research files. Never pushed. |
| C — GitHub `PruvCode/Trace` | `main` @ `eb467f89…`, tag `v0.1.0` → `a177803`. No Claude branch on remote at takeover. |

WIP import fidelity: Claude's `d825c8c` is content-identical to the original WIP on all
28 changed paths (newline-normalized). (Earlier 9/28 "mismatches" were LF/CRLF hash
artifacts.)

## adopt-with-audit — Claude's changeset (audit state)

| Claude change | Audit result |
|---|---|
| `d825c8c` WIP import | VERIFIED faithful (28/28). **Adopted into `recovery/trace-2026-10-29`** @ `2b72a71` after re-verification + secret/privacy scan (clean). |
| Per-request injection (D3, plugin) | Mechanism verified plausible; **independent A/B probe running** (`research/phase0/probe_injection_policy.py`). Not yet adopted. |
| Task-aware retrieval C3 (session_context/message_context) | Code audited; env-gated (`TRACE_TASK_AWARE=0` → C2). Behind Phase 2 evidence. Not yet adopted. |
| `sanitize_pairs.py` + Phase-1 outputs | Permission-D overreach CONFIRMED: opened per-project `.agent-memory/memory.db` stores read-only (counts only, no content, no writes). Documented; will not repeat. Labels are AI-assisted (Claude subagents), NOT human; human review still pending (blocker B2). |
| Phase-2 harness (`run_phase2.py`) | Audited; partial run data exists (run1.jsonl: C2/C3 passes=on, C0/C1 failures=fine). Full evaluation NOT RUN. Mechanism-level per-request behavior visible in Claude's own logs (C2/C3 rows: `injected==requests`). |
| Test edits (3 memory test files + test_analyze) | test_analyze one-liner is a workaround for a root cause we fixed properly (see DEF-1). Memory-test edits accompany the injection change → will be audited + re-validated before adoption. |
| Fixture copies (fixtures/, 14 repos incl django clone) | NOT adopted; root-cause fixed by tracking synthetic fixture sources instead (DEF-1). |

## Phase 0 — reconcile & stabilize (state)

| Item | Status |
|---|---|
| Recovery branch | DONE — `recovery/trace-2026-10-29` @ `2b72a71` pushed to GitHub. |
| DEF-1 clean-checkout fixtures | FIXED — commit `246eea2` (track 12 source-backed fixtures) + `6aace71` (history fixture untracked; seeder owns its source). Pins re-verified. |
| `test_analyze` failure | ROOT-CAUSED (empty task mapping from unseeded fixtures), not order dependence. |
| Full deterministic suite, clean checkout | **PASS — 438/29/1(0) pre-adoption; 439/29/1(0) after adoption**, zero failures. |
| Injection policy A/B probe | **DONE** — persistence premise verified (never persisted), A=1/9+1/8 injections, B=5/5, no dups; semantic recall inconclusive (weak model; honest record). Policy B adopted. |
| Memory-test adoption (Claude's 3 test files) | DONE — audited (no weakening; policy-faithful re-encodings), 51 passed/1 xfailed focused, full suite green. |
| Compatibility matrix | OpenCode 1.18.32 disposable VALIDATED E2E (capture, plugin injection, retrieval) again this session; 2.1.4 (installed) NOT RUN. |

## Pushed to GitHub (verified via ls-remote)

| Branch | Local SHA | Remote SHA | PR |
|---|---|---|---|
| `recovery/trace-2026-10-29` | `2b72a71` | same | open when phases merge |
| `phase/0-reliability` | `6aace71` | same | open when phase completes |

## Known blockers (unchanged from takeover)

- B1 paid-model experiments: NOT authorized.
- B2 human review of 5 pilot pairs: PENDING (no `review_outcomes_keyed.json`).
- B3 OpenCode 2.x compatibility: NOT RUN.
- B4 Claude Code integration: NOT RUN.
- Phase-2 full evaluation (C0–C3): NOT RUN; harness validated, A-phase partial only.

## Update, 2026-10-30

Current state, rating and corrections to this ledger: `CURRENT_STATE_AUDIT.md`. Phase 2 has run;
the blockers above still hold, and live model runs are also blocked by the free-tier limit.

## Update, 2026-10-31

Security, redaction, the startup delay, OpenCode 2.x and CI are done. Phase 3 is preregistered and waiting on the free
model's usage limit. Latest detail: `TEST_RESULTS.md`, `DECISIONS.md`, `FINAL_REPORT.md`.
