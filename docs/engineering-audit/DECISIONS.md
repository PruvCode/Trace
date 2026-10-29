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

**GLM-D6 — Phase 1 adopted as evidence, not as completed research.** `phase/1-research`
carries Claude's committed `docs/autopilot/` decision/evidence record + its final
uncommitted edits, and `research/phase1/` (sanitizer + capture audit + AI-assisted
labels), all verbatim with a `PROVENANCE.md` reading guide (status semantics,
supersessions, Permission-D caveat). Positions taken: (a) pilot labels are AI-assisted
only and reviewers disagree — no human ground truth exists yet (B2 pending); (b)
Phase-1 synthetic-material capture/retrieval quality evidence already in-tree is the
deterministic suite + the Phase-0 probes; no new private-store access is authorized or
performed; (c) Claude's B5 `fixtures_src/` recommendation is superseded by the Phase-0
in-place root-cause fix; (d) anything REPORTED-not-reexecuted (E7, E8) stays labeled as
such and cannot anchor release claims.

**GLM-D7 — Phase-2 run dispositions, fixed BEFORE the run (2026-10-29, staged on
`phase/2-eval` before the full preregistered run produced any outcome row).**
(a) The preregistration is binding as written (10 tasks, C0–C3, reps=2, seed 20261028,
shuffles per task, hidden eval scoring, task-cluster bootstrap 10k, ±30–40 pp
distinguishability, smaller diffs reported INCONCLUSIVE).
(b) Validation smoke runs are engineering-only, written to a separate root
(`%TEMP%\trace_p2_smoke`), and EXCLUDED from the analysis dataset — they are junked
after log-level sanity checks and never pooled with the preregistered data.
(c) No third rep is planned (decided now, before outcomes; the prereg reserves a third
rep only for time-allowing cases, decided before looking at outcome differences).
(d) Workers=2 (interleaved tasks; prior agent ran the same way; per-task isolation is
by separate workspaces/homes).
(e) The prior agent's partial `run1.jsonl`/`smoke.jsonl` are kept as provenance-only
prior data and are NOT pooled into the dataset or the analysis.
(f) Analysis uses the adopted `analyze_phase2.py` run ONLY on the GLM full-run JSONL.
