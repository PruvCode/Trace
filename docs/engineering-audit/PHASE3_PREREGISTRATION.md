# Phase 3 preregistration (written before any outcome)

Harness: `research/phase3/run_phase3.py` at the commit that adds this file. Agent: OpenCode 2.2.5 in an
isolated environment with `--standalone`, model `opencode/big-pickle` (free). No other model.

## Design

- Six new tasks, not used for tuning. Each needs a fact stated only in session 1, which the code cannot reveal.
- History per task: session 1 states the fact, sessions 2–5 do unrelated reading.
- Primary history: every session is told to append a handoff to `AGENTS.md`. This is the best case for notes.
- Session B arms, 2 reps each, order shuffled by fixed seed:
  - N0: no memory.
  - N1: the accumulated `AGENTS.md`.
  - T1: previous TRACE default: recency retrieval, all nine MCP tools.
  - T2: current TRACE default: task-aware retrieval, four tools.
- Secondary history: same sessions with no handoff instruction. Arms T2U (TRACE) and N1U (whatever notes exist).

## Outcomes

Primary: B passes the hidden check. Secondary: input tokens, wall seconds, memory-tool calls, injected characters,
whether the fact reached the notes and the store. Timed-out rows with zero tool calls are reported separately as void.

## Hypotheses

- H1: N1 > N0. H2: T2 > N0.
- H3: T2 > T1 (the optimization matters once the fact is outside the recent tail).
- H4: T2 ≥ N1 on correctness (TRACE is at least as good as best-case notes).
- H5: T2 uses no more input tokens than T1.
- H6: T2U > N1U (TRACE keeps knowledge when nobody asks for notes).

## Analysis

Pass rate per arm and a task-cluster bootstrap (10 000 resamples) of paired differences. With six tasks and n=12 per
arm, differences under about 40 points are inconclusive and reported as such. No third rep.

## Amendment before any outcome

A smoke run showed an agent walking up the directory tree and reading the earlier session's `AGENTS.md` and OpenCode
database. Phase 2's layout had the same exposure. Session B now runs in a random folder under a separate root with a
fresh OpenCode home. A run whose tool calls leave the workspace is flagged `escaped`; results are reported with and
without flagged runs. Smoke data is excluded from analysis.

## Second amendment before any valid outcome

The first full attempt produced no usable data: `big-pickle` hit `Free usage limit reached` and two capture bugs
were found and fixed. That run was discarded. The model for the rerun is `opencode/nemotron-3.5-lightning-free`,
the only free model still answering, so results are not comparable with Phase 2. Primary arms now finish before
any secondary arm. The code under test is the commit recorded in the run's first line.

## Known limitations, stated in advance

- Synthetic tasks on tiny fixtures, written by the same author as the product.
- Task prompts hint that a decision exists, and session 1 is prompted, not organic.
- One free model. The provider's usage limit may void rows; the secondary arms run last for that reason.
- Standalone mode, not the OpenCode background service.
- Notes in N1 depend on the agent writing them; that is measured, not forced.
