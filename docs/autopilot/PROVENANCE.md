# Provenance (GLM-5.3, 2026-10-29)

`docs/autopilot/` and `research/phase1/` on `phase/1-research` are adopted verbatim from
the prior agent's local-only branch (`autonomous/trace-research` @ `4363ff42`, plus its
final uncommitted edits to four docs files). They are research evidence and a decision
record from an earlier, separate effort — adopted with audit, not re-executed.

Reading rules:

1. **Statuses are the prior agent's self-assessments.** `VERIFIED` items E1–E5, E6, E9,
   E10 were re-derived or confirmed independently during the GLM takeover audit.
   `E6`'s recall numbers (9/10) are treatments run by that agent; the GLM Phase-0 probe
   re-verified the *mechanism* claims only (injection spread, non-persistence) and
   recorded semantic recall as INCONCLUSIVE — no product effectiveness claim rests on it.
   `REPORTED` items (E7, E8) were not independently re-executed by GLM.
2. **Superseded content.** `BLOCKERS.md` B5 (DEF-1, "move fixture sources under
   `fixtures_src/`") is closed the different way: the root cause was fixed in place on
   `phase/0-reliability` (`246eea2` tracks the 12 source-backed fixtures; `6aace71`
   untracks `fixtures/history_repo` whose seeder owns its source). No re-adding of
   removed content beyond that fix was needed.
3. **Phase-1 labels are AI-assisted, NOT human ground truth.** Both
   `research/phase1/reviewer_{A,B}_labels.json` carry `"kind": "AI-assisted (Claude
   subagent), NOT human"`, and the two reviewers disagree on several pairs. Human
   five-pair review is still pending (blocker B2; no `review_outcomes_keyed.json`).
4. **Permission-D caveat.** `research/phase1/sanitize_pairs.py` opened per-project
   capture stores read-only (COUNT-only capture audit; privacy lint guarded the
   emitted label material). The access crossed the stated Permission-D boundary; the
   audit found no content read, no writes, and content-free outputs. Recorded in
   `docs/engineering-audit/DECISIONS.md` (GLM-D4) and not repeated.
5. **Referenced artifacts** (track bundles, trial logs, review HTML, manifests) live on
   the prior agent's workspace, not in this repo, except where copied under
   `research/`; anything not in-tree cannot be checked here.
6. **Phase-2** harness/preregistration is adopted separately on the Phase-2 branch and
   the preregistration is binding for any run made from it.
