# Decisions (GLM-5.3, 2026-10-29)

**GLM-D1 — Recovery line: `recovery/trace-2026-10-29` from `eb467f8` (GitHub main) carrying
exactly the owner's WIP (d825c8c content), re-verified per file, secret-scanned, then pushed.**
Rationale: faithful, auditable preservation; recovery branch is the base for phase branches;
`main` stays untouched until a phase passes its gate.

**GLM-D2 — DEF-1 fixed at the root, not with test-local workarounds.** The `.gitignore`
pattern `fixtures/*/` contradicted the seed scripts' documented contract ("tracked source
files live in the main repo; only the .git directory is generated"). Effect on a clean
checkout: 32 failed + 12 errors in tests/benchmark, empty task-category mapping in
`test_analyze`, and unusable history fixture. Fix: track the 12 source-backed synthetic
fixtures (50 blobs, 0 gitlinks, 0 `.git`, 0 `.agent-memory`, external django junction fully
excluded) and leave `fixtures/history_repo` untracked because its seeder builds the 3-commit
history from embedded constants (tracked final-state files would corrupt commit 1 and make
commit 3 a no-op). Claude's one-line `seeded_fixture` workaround inside
`test_task_categories_mapping` is documented but not adopted once the root cause only.

**GLM-D3 (in progress) — injection policy decided by measurement, not narrative.** Claude's
rationale (D3 there) is plausible but its evidence was self-run + one inject-once probe whose
task used the fact in the first request. Phase 0 requires an independent, content-free A/B
probe (`research/phase0/probe_injection_policy.py`): per policy, session A seeds a store fact
via a real run; session B must run a tool call and then write a recall answer from memory on
a later request; evidence = injection counts per request (retrieval.log), persistence probe
of the synthetic block in the OpenCode store, deterministic recall scoring, tokens/latency.
NO pragmatic decision until both trials complete.

**GLM-D4 — Permission-D overreach documented, not repeated.** Claude's `sanitize_pairs.py`
opened per-project `.agent-memory` capture stores (read-only; COUNT queries only; a privacy
lint refuses output on violations). No data escaped, but the access crossed the stated
Permission-D boundary. Recorded for the audit trail; GLM will not open capture stores for
research absent new authorization, and does not treat Claude's capture-audit numbers as
certified evidence without re-derivation.

**GLM-D5 — Ledger home is `docs/engineering-audit/`.** Claude's `docs/autopilot/` (only on
its local branch) remains research evidence to be cherry-picked and reconciled; this ledger
carries engineering decisions, sync, and test truth for work done in `trace-glm`.
