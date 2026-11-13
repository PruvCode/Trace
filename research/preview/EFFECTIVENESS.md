# TRACE coding effectiveness

## Local diagnostic

Use the existing two qualification cases and the five unscored two-file tasks in the preview benchmark. Keep the earlier benchmark files and results intact. Use the installed loopback Gemma 3 4B at its recorded digest, with seed 7093, temperature zero, a 4,096-token context, and at most 512 generated tokens. Use the same two-file JSON response schema and review instruction for every arm. This separates response-format errors from wrong code while keeping the model and rules fixed across the comparison. The previous result used a different prompt and response format, so it remains a separate run.

Run the host and resource boundary checks. The executable grader must reject all seven original implementations and accept all seven reference implementations. Give the model the complete historical rule for the two qualification tasks. Require both to pass. If either fails, stop scoring after two model requests and regrade the saved proposals. If both pass, run each of five held-out tasks once against no memory, a prepared note, audited TRACE at `9e54673`, and the current TRACE code. The arm order follows the existing frozen seed. There are at most 22 model calls, all local. The model receives source and bounded history, with no tools or grading files. The grader runs proposed files inside WASI.

Report exact executable passes by task and arm, valid output, required facts present in context, token counts and wall time. Regrade every proposal without inference. A single synthetic comparison is diagnostic evidence.

## Representative test requirements

| Stage | Required checks | Acceptance |
| --- | --- | --- |
| Validate each fixture | Freeze the repository revision, task prompt, allowed tools, historical facts, and executable acceptance and regression tests. Include one known-good patch and the unchanged checkout. | Every known-good patch passes. The unchanged checkout fails the new acceptance tests. Existing regressions are documented and held constant. |
| Qualify the model | Use disjoint multi-file tasks with the required facts supplied. Give every arm the same editing, tool and time budgets. | The model meets a threshold fixed in advance on each model configuration. Runs from an unqualified model stay outside the comparison. |
| Verify memory delivery | Seed identical raw histories in disposable profiles. Check cross-session recall, task separation, superseded decisions, uncommitted changes and on-demand evidence. | The expected records surface within the context budget. Wrong-project and private records stay out. Stale or ambiguous evidence is identifiable. |
| Measure real coding | Start with at least 24 held-out tasks across three repositories, two qualified models and three repeats. Cover multi-file debugging, interface changes, repeated investigations and ordinary tasks that need no history. Include Rubaru-style web work and Python projects. | Run matched no-memory, maintained-notes and current-TRACE arms, plus an earlier TRACE arm where useful. Randomize arm order, start every trial from the same clean revision, and keep grading hidden. |
| Measure side effects | Record executable pass rate, regressions, bad changes, wrong-memory usage, tokens and elapsed time. Compare effort to record and maintain notes with TRACE's capture effort. | Publish matched results and task-clustered uncertainty intervals. Count timeouts and invalid edits as failures. Report capture and inference costs separately. |

For a stable-release claim, set the minimum practically useful lift before the held-out runs. A proposed bar is a 10-percentage-point increase in executable passes over maintained notes, with a paired 95% interval excluding zero and no increase in serious regressions or disclosures. Expand the sample if the interval remains inconclusive. Run a separate independent pilot to measure what people actually write in notes and what they retrieve during real work.

Model downloads and paid inference are separate decisions. The local diagnostic uses the installed model and synthetic histories. Do not connect real OpenCode profiles to the benchmark.

## TRACE 2.0 matched diagnostic

`python -m research.preview.trace2_evaluation --previous PRE_CHANGE_SOURCE --wasi WASI_ROOT --scratch SCRATCH`
uses the pinned local Qwen2.5-Coder 7B model, two qualification cases and eight
scored tasks. The previous TRACE arm uses the Phase 2 source and a separate
database with the same fixtures as TRACE 2.0. Three tasks include a recorded
superseded transcript and routine updates. Maintained notes contain the full
approved decision, and the no-memory arm receives the task and code only.
The runner randomizes arm order using the fixed seed, keeps budgets identical,
and writes decisions present, obsolete text present, evidence references,
executable verdicts, tokens and time as JSONL. `--context-only` makes no
model requests, and `--gemma --qualify-only` checks the second local model.
Synthetic diagnostics support product decisions and supplement the
representative release gate above.

## Matched verification trial

`python -m research.preview.verification --wasi WASI_ROOT --scratch SCRATCH`
uses six fresh two-file fixtures and the same pinned local Qwen model. The
no-memory, maintained-notes and TRACE arms use the same public assertion and
can retry once if that assertion fails. TRACE recalls a captured test identifier
and the approved decision from a disposable task. Notes carry the same facts.
The remaining assertions stay hidden and score the final candidates. The runner
records the candidates, diagnostics, verdicts, tokens and wall time as JSONL.
Use `--preflight-only` to validate fixtures and memory without inference, or
`--regrade RESULTS` to check saved verdicts without new model requests.

`--boundary-followup RESULTS` is a separate diagnostic on four known failures.
It reuses each memory arm's first candidate and permits one correction using
up to three public probes. The added probe inputs are distinct from the hidden
checks. Keep these follow-up results separate from fresh-task scores.

`python -m research.preview.definition_trial --wasi WASI_ROOT --scratch SCRATCH`
uses the six previously evaluated tasks to diagnose definition-guided edits.
It gets definitions and decision excerpts from a disposable TRACE project,
then requests one edit per definition with the other module visible. The
existing hidden grader scores the combined patch. This is a matched replay
with a different edit strategy and two calls per task, so it cannot isolate
the effect of retrieval or establish a fresh-task win over notes.
`--public-probes` constrains model output to one function at a time and tests
each proposed function against separate public examples before accepting it.
This uses the same hidden grading cases and remains a diagnostic replay.

## Fresh matched definition trials

`python -m research.preview.matched_definitions --wasi WASI_ROOT --scratch SCRATCH`
compares eight new synthetic tasks with the same editor, public examples,
retry limit and grading in each arm. The notes arm sees the full approved
decision for each edit. TRACE extracts the relevant passage from that same
decision using its current source index. `--resume PREVIOUS_LOG` preserves
completed results after an interruption. `--regrade RESULTS` rechecks
saved patches and reports paired wins, losses and a bootstrap interval.

`research.preview.repository_python` checks two fresh tasks on pinned TRACE
functions in WASI. `research.preview.repository_js` checks four tasks on pinned
Rubaru and WISIT files with an AST-restricted edit and a bounded QuickJS WASM
runtime. The latter takes the TypeScript compiler path and the directory of an
isolated `quickjs-emscripten@0.32.0` install. Each runner has `--preflight-only`
and `--regrade RESULTS` modes. These are repository-source function tests.
Application builds, browser flows, normal agent tool use and the wider release
gate are additional evidence.
