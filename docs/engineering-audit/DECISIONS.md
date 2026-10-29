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

**GLM-D3 — FINAL: adopt per-request injection (Claude's baedd4b memory code + tests) on
`phase/0-reliability`; reject once-per-session.** Evidence: independent probe
(`research/phase0/probe_injection_policy.py`, jsonl evidence in-repo) — OpenCode never
persists the synthetic block (verified under both policies via message/part-store scan);
once-per-session thus structurally loses memory from request 2 onward (observed 1/9 and
1/8 injection spread), while per-request supplies memory every request with dedup (5/5)
and bounded overhead (`MAX_CHARS`, helper cached ~1 spawn/second). Claude's rewritten
tests were audited for weakening and judged faithful re-encodings of the policy (including
`sum(spawned) == 2` and leak checks). Semantic recall remains INCONCLUSIVE (weak free
model failed the recall artifact under both policies; no product claim made from it) —
proper semantic A/B belongs to Phase 2 arms, not this probe.

**GLM-D4 — Permission-D overreach documented, not repeated.** Claude's `sanitize_pairs.py`
opened per-project `.agent-memory` capture stores (read-only; COUNT queries only; a privacy
lint refuses output on violations). No data escaped, but the access crossed the stated
Permission-D boundary. Recorded for the audit trail; GLM will not open capture stores for
research absent new authorization, and does not treat Claude's capture-audit numbers as
certified evidence without re-derivation.

**GLM-D5 — Ledger home is `docs/engineering-audit/`.** Claude's `docs/autopilot/` (only on
its local branch) remains research evidence to be cherry-picked and reconciled; this ledger
carries engineering decisions, sync, and test truth for work done in `trace-glm`.
