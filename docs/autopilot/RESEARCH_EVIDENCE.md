# Research evidence

Labels: VERIFIED (re-run or recomputed here) · REPORTED (artifact from an earlier session, read not recomputed) · INFERRED · DOCUMENTED (vendor docs) · UNVERIFIED · NOT RUN · BLOCKED.

## Evidence inventory

| # | Claim | Source | Label |
|---|---|---|---|
| E1 | Real OpenCode store: 1005 sessions, 8854 messages, 33892 parts | t2_inventory_results.json, track_1a | REPORTED (matches my own count from the reliability phase → VERIFIED) |
| E2 | 883 sessions in temp-like dirs; 122 non-temp; 79 genuine dev candidates + 22 uncertain (excluded) | t2_inventory_results.json | REPORTED |
| E3 | 79 consecutive same-project pairs (earlier ledger said 75; keyed file has 79) | pair_features_keyed.json | VERIFIED (recounted) |
| E4 | Strict repeated failed command (identical input hash, error in both): 0/79 pairs | pair_features_keyed.json | VERIFIED (recomputed) |
| E5 | Identical successful tool input in both sessions: 26/79 pairs, 279 incidents | same | VERIFIED |
| E6 | 10 trials t11–t20: exact recall 9/10 (8 via injection), control 0/3 each | my trial harness outputs | VERIFIED (I ran them) |
| E7 | Hook continuity probes (track_1c) on OpenCode 1.18: injection works; inject-once passed one probe | track_1c_*_results.json | REPORTED |
| E8 | Git evidence durability audit: 34/34 assertions pass (fixture repo only) | trace_p1_gitdurability/evidence_durability_audit5.log | REPORTED. Fixture-level only; per directive this alone does not justify shipping a git snapshot mechanism. |
| E9 | Pilot sessions captured by TRACE | sanitize_report.json | VERIFIED: 0 of 10 |
| E10 | Installed `opencode` is v2.1.4; TRACE live-validated only on 1.18.x | `opencode --version` | VERIFIED |
| E11 | Claude Code hook semantics (UserPromptSubmit, PreToolUse context) | code.claude.com docs | DOCUMENTED |

## Phase 1 — selection bias (VERIFIED, content-free)

- 79 pairs → 44 insufficient on either side, 31 near-simultaneous (<180 s), **24 eligible pool from only 4 project groups**.
- Selection ranked the pool by (p2, retry, p1, path_overlap); with p2 = retry = 0 everywhere it reduces to **top-5 by repeated-read count**.
- p1 (identical successful inputs): all pairs median 0 (53/79 zero); pool median 2.5; **selected median 20** (20,20,20,20,21). The 5 selected pairs hold 101/279 incidents.
- Selected pairs span 3 groups; pair4.earlier and pair5.later are structurally identical (very likely the same session), pairs 2/3 share one project. Effective independent evidence ≈ 3 project chains.
- Two pairs with a shared tool error (`same_tool_err_count=1`) were not selected: one with a 5-day gap and p1=0; one with a 214 s gap and two read-family repeats. Neither shows a repeated failed command under strict hashing.
- Conclusion: the pilot sample is the most favourable slice for memory; any prevalence estimate from it is an upper bound and not generalisable.

## Phase 1 — AI-assisted pilot review (NOT human annotation)

Input: locally sanitized, content-free timelines (`research/phase1/sanitize_pairs.py`; privacy lint 0 violations; raw text, paths, ids never left the machine; excerpts kept outside the repo). Two Claude subagents, separate contexts, same rubric.

| pair | P1 A/B | P2 A/B | helps A/B | steps saveable A/B |
|---|---|---|---|---|
| 1 | yes/yes | no/no | yes/yes | 10/15 |
| 2 | unclear/yes | unclear/yes | unclear/yes | 3/12 |
| 3 | yes/yes | unclear/yes | yes/yes | 8/8 |
| 4 | unclear/yes | no/no | unclear/partial | 4/5 |
| 5 | yes/yes | no/no | yes/partial | 6/6 |

Raw agreement: P1 3/5, P2 3/5, helps 2/5 (n=5; kappa not meaningful). No disagreement is a yes/no flip; all are yes vs unclear/partial.

What both reviewers saw:
- Re-orientation re-reads of the same file set at the start of later sessions (pairs 1, 3, 5), partly unavoidable (read-before-edit, changed files).
- Reviewer B: pair 3 repeats an **environment/procedure failure sequence** (plain `python -m` fails → switch to venv exe → full test suite times out → `--collect-only` rejected → targeted test file), invisible to strict hashing because the interpreter path token changed. Pair 2 repeats a long experiment run that times out under the tool limit.
- Estimated saveable steps are modest (A: 31, B: 46 across ~363 later-session tool calls ≈ 9–13 %) and mostly orientation reads.

Reviewer caveats (kept): err classes come from keyword matches on shell output (false positives possible); "rejected" may be a user permission decline; user intent invisible; P3 (staleness) not judgeable.

## Phase 1 — capture-quality audit

Ground truth first (OpenCode part counts per session), then TRACE stores at each session's project directory, opened read-only:
3 pairs: store present, **0 captured rows** for all 6 sessions; 2 pairs: no store. Capture coverage of the pilot = 0/10 sessions (the sessions predate or bypass TRACE capture). Precision/recall of capture on real work cannot be measured from history → only from live controlled runs (Phase 2). Ownership: stores live inside the session's own project directory; content was never read (counts only).

## Phase 1 decision

Real-history evidence supports a narrow claim: later sessions often **re-orient** on the same files, and sometimes repeat **environment/procedure dead-ends**. It does not show frequent repeated failed approaches (0/79 strict; 2/5 per one AI reviewer on a biased sample). The knowledge most often worth carrying is short procedural/environment notes — the thing an agent-maintained AGENTS.md also carries. → Phase 2 must compare TRACE against an AGENTS.md baseline, not only against no memory.

## Phase 2 results

Two complete runs of the preregistered design. Summary and caveats: `docs/engineering-audit/CURRENT_STATE_AUDIT.md`.
Reproduce: `python research/phase2/analyze_phase2.py research/phase2/run1.jsonl` (or `run2_glm.jsonl`, or both for the pooled table).

Run 1 is the preregistered analysis. Run 2 is a separate agent's replication at the same runtime code. Pooling was not preregistered and is exploratory.

| C_episodic per task, passes (C0 / C1 / C2 / C3) | Run 1 of 2 each | Run 2 of 2 each |
|---|---|---|
| task_01 failed attempt | 2 / 2 / 2 / 2 | 2 / 2 / 2 / 2 |
| task_02 decision | 0 / 2 / 2 / 2 | 0 / 2 / 2 / 2 |
| task_03 incident blocklist | 0 / 2 / 1 / 1 | 0 / 2 / 1 / 1 |
| task_04 reverted cache key | 2 / 2 / 2 / 2 | 1 / 2 / 1 / 2 |
| task_05 cursor rollout | 2 / 2 / 2 / 2 | 0 / 0 / 2 / 1 |
| task_06 migration | 0 / 1 / 1 / 2 | 0 / 1 / 2 / 1 |

Task-cluster bootstrap 95% CIs on C_episodic, percentage points:

| Difference | Run 1 | Run 2 | Pooled |
|---|---|---|---|
| C1 − C0 | +42 (8, 75) | +50 (17, 83) | +46 (13, 79) |
| C2 − C0 | +33 (8, 67) | +58 (25, 92) | +46 (17, 75) |
| C3 − C0 | +42 (8, 75) | +50 (25, 75) | +46 (21, 75) |
| C2 − C1 | −8 (−25, 0) | +8 (−33, 50) | 0 (−25, 25) |
| C3 − C1 | 0 (−25, 25) | 0 (−25, 25) | 0 (−21, 17) |
| C3 − C2 | +8 (0, 25) | −8 (−33, 17) | 0 (−13, 13) |

Hypotheses: H1 supported. H2 supported. H3 not supported. H4 passed in run 1 (8/8 every arm) but weakly tested, and void in run 2. H5 **not supported**, inconclusive at this sample size.

Failures and voids. Run 1: 7 of 10 failures involved a timeout or error exit, so some are model stalls, not missing knowledge. Run 2: 36/80 rows timed out, 32 of them with zero tool calls, most likely from the provider's `Free usage limit reached`, which a direct probe confirmed on 2026-10-30 after the run ended. Those rows count as failures in the preregistered tables. Post-hoc removal of the 33 void rows gives C_episodic C0 41%, C1 83%, C2 87%, C3 95%, and D tasks 100% in every arm (n=9–10).

Capture: key facts in the store in 18/20 Session A runs; in AGENTS.md in 15/20. One store miss was probably redaction removing a credential value. Agents recorded 1 finding in 20 sessions. Session B called memory tools in 6/80 C2/C3 runs.
