# Phase 2 preregistration (written before any Phase 2 result)

Harness: `research/phase2/run_phase2.py` at the commit that adds this file.
Agent: OpenCode 1.18.32 (pinned disposable install), model `opencode/big-pickle` (free, $0). No other model.

## Design
- 10 tasks: C_episodic 01–06 (knowledge told to / discovered by Session A is needed or helpful in B), D_staleness 01–04 (A ran on an older code state; what A learned is now wrong).
- Per task one shared Session A (asked to append a handoff to AGENTS.md; TRACE captures it). Then Session B with the unchanged task prompt in C0 (none), C1 (A's AGENTS.md), C2 (TRACE recency injection), C3 (TRACE task-aware injection).
- Workspaces: `git archive` of the fixture at the task's base commit (no git history), `# BUG` comments stripped, so prior knowledge reaches B only through the condition.
- Condition order shuffled per rep with a fixed seed. Planned reps: 2 per condition per task (80 B runs), a third if time allows (decided before looking at the outcome differences).

## Outcomes
- Primary: B passes the task's hidden evaluator.
- Secondary: B tool calls, wall seconds, tokens; injected chars; whether A's handoff / TRACE store held the key fact.

## Hypotheses
- H1: C1 > C0 on C tasks.  H2: C2 or C3 > C0 on C tasks.  H3: C3 ≥ C2.
- H4 (harm): on D tasks no memory arm passes less often than C0.
- H5 (product question): C2/C3 > C1 on C tasks. TRACE's automatic capture is justified over an agent-maintained notes file only if H5 holds.

## Analysis
Pass-rate per arm; paired differences vs C0 and vs C1 with a task-cluster bootstrap (10 000 resamples, tasks resampled, reps kept inside a task). With 10 tasks this is a pilot: only differences of roughly ≥ 30–40 pp are distinguishable from noise; smaller differences are reported as inconclusive, not as "no effect".

## Known limitations (stated in advance)
- Synthetic tasks on tiny fixtures; Session A is prompted, not organic.
- One free model; results may not transfer to stronger models.
- C1 depends on A actually writing a good handoff (measured, not forced).
- Same author designed tasks and TRACE; tasks were taken from the existing corpus, prompts for A written here.
