# TRACE maintained-notes milestone audit

**Retrieval improved in the tested scenarios. The coding-effectiveness
milestone remains open.** The project rating stays at **6.5/10** on the scale
used for this milestone.

## Scope and fixtures

The baseline is merged main at `eb049d4`. The patch is in [PR #6](https://github.com/PruvCode/Trace/pull/6).
The [frozen trial](../../research/preview/EFFECTIVENESS.md#maintained-notes-repository-function-trial)
uses 24 new tasks drawn from pinned TRACE, Click, and Packaging functions.
Three tasks require no historical context. The 21 remaining tasks use authored
project histories with an approved decision, a superseded decision, another
task's decision, and 36 newer observations per target.

Every unchanged definition failed its requested behavior check and passed its
preservation check. All reference changes passed both. The same checks passed
for the three disjoint qualification fixtures. The bounded WASI grader verified
that it could not see the host canary and rejected an infinite loop.

## Retrieval and capture findings

| Check | Previous main | Patch |
| --- | ---: | ---: |
| Approved decisions delivered | 0/21 | 21/21 |
| Matching evidence references delivered | 0/21 | 21/21 |
| Wrong-task or superseded markers in recall | 0/24 | 0/24 |

The candidate limit was applied in recency order before ranking. On a file with
many recent work logs the correct decision never reached the final ranker.
TRACE now prioritizes exact definitions and evidence-backed decisions before
limiting candidates, and preserves that priority when filling the prompt.
Recall also filters transcript evidence linked only to superseded events,
including when the prompt falls back to recent conversation. A regression test
covers an older decision, 60 recent observations, a same-file sibling, and a
superseded decision.

Native hooks now create the same bindable session markers used by OpenCode.
This lets task-bound Codex, Claude Code, and Antigravity sessions use the
existing task evidence path. Existing native sessions get a marker on the
next captured message. This work adds no collection of raw command output.

The existing synthetic scale check passed at 1,000, 10,000 and 50,000
messages. At 50,000 messages and 10,000 events, both revisions recovered all
four targets. Concurrent CPU inference and tests made those timings unsuitable
for a latency comparison. All three no-history controls received a nonempty
memory block. One reconstructed control included an approved note for a
different function in the same file. The marker check in the table covers the
seeded wrong-task and superseded records, and does not measure all irrelevant
context.

## Coding-effectiveness gate

The frozen runner uses one signature-preserving edit per arm, the same source
and prompt limits, a randomized arm order, and checks unavailable to the
model. Maintained notes supply the complete approved rule. TRACE receives
automatic recall. The no-memory arm receives the task and code.

| Model | Qualification | Result |
| --- | ---: | --- |
| Qwen2.5-Coder 7B | 3/3 | Eligible for held-out scoring |
| Gemma 3 4B | 2/3 | Excluded from held-out scoring |

Independent regrading reproduced all six frozen qualification verdicts.
Two earlier prompt-format experiments were development probes and did not
score held-out tasks.

### Held-out coding outcomes

| Completed tasks | TRACE | Maintained notes | No memory |
| --- | ---: | ---: | ---: |
| Original batch | 0/17 | 1/17 | 1/17 |
| Resumed batch | 0/7 | 0/7 | 0/7 |
| All tasks | 0/24 | 1/24 | 1/24 |
| History-bearing tasks | 0/21 | 0/21 | 0/21 |

The only successful change was the same self-contained TRACE budget task in
the notes and no-memory arms. The exploratory difference was −4.2 percentage
points versus notes. The result cannot establish a repeatable effect in
either direction.

| Outcome or cost | TRACE | Maintained notes | No memory |
| --- | ---: | ---: | ---: |
| No accepted edit | 16 | 15 | 15 |
| Accepted edit failed the preservation gate | 3 | 3 | 0 |
| Accepted edit preserved behavior and missed the requested check | 5 | 5 | 8 |
| Median input tokens | 523 | 220 | 193 |

Independent regrading reproduced all 72 held-out verdicts. The local process
stopped after 17 completed tasks. The original verdicts and accepted edits
were retained. The remaining seven tasks used the same model,
seed, pinned source, and grader, with TRACE context rebuilt from the preserved
trial databases. The rebuilt TRACE text matched 16 of the 17 saved prompt
context hashes. The one mismatch belongs to the original batch and was left
unchanged. The batch totals are reported separately because context
reconstruction could not establish identical execution conditions.

The single-function, single-repeat trial exposed a weak editor baseline.
Qualification on three simple edits did not predict held-out performance.
The predeclared representative gate requires repeated multi-file tasks, two
qualified models, and a measured lift over notes.

### Recovered fenced-editor replay

A fenced-prompt replay of the same 24 tasks at the patch head finished writing
all 72 verdicts shortly before the working session was interrupted by an
inference-quota limit, and was never summarized. The preserved output and run
context were recovered from the work directory and independently regraded with
the current code: all 72 verdicts reproduced.

| Completed tasks | TRACE | Maintained notes | No memory |
| --- | ---: | ---: | ---: |
| History-bearing tasks | 2/21 | 3/21 | 0/21 |
| No-history controls | 1/3 | 1/3 | 1/3 |
| All tasks | 3/24 | 4/24 | 1/24 |

The exploratory difference was −4.2 percentage points versus notes (paired
bootstrap 95% CI [−12.5, 0]).

| Outcome or cost | TRACE | Maintained notes | No memory |
| --- | ---: | ---: | ---: |
| No accepted edit | 15 | 15 | 15 |
| Accepted edit failed the preservation gate | 1 | 1 | 0 |
| Accepted edit preserved behavior and missed the requested check | 5 | 4 | 8 |
| Median input tokens | 390 | 207 | 181 |

Memory changed coding outcomes on exactly two history-bearing tasks
(`_parse_keywords` and `_identifier`: both memory arms passed, no memory
missed both). The entire trace-versus-notes gap is one task (`is_git_path`,
notes-only pass). On 15 of 24 tasks every arm produced no accepted edit, and on
20 of 24 tasks no arm passed, so acceptance is aligned across arms and the
one-shot editor remains the binding constraint rather than recall.

### Editor mechanics, v2, and the requalification decision

A deterministic raw-output probe on the replay's failures showed the mechanical
cause: the model legitimately re-emits supporting definitions (`import re`,
constants, small helper classes) inside its fenced block, and the v1
function-only extractor rejected the extra top-level statements, discarding
complete replies. Editor v2 (in `research/preview/editor.py`, uncommitted at
this revision) adds strict-then-fallback mechanical extraction and one
equal-budget mechanical retry with defect feedback, applied identically in
every arm and never showing graded checks. The runner gained `--editor-v2`, an
evidence-linked excerpt arm (`trace_min`), a durable `--output` JSONL, and a
per-row `defect` field. Six unit tests cover the extraction and retry
mechanics; the fenced replay regraded unchanged under the new code, and
rebuilt recall blocks matched the frozen blocks semantically (21/21 decisions
with evidence, three empty controls).

A first launch of the development probe surfaced a scheduling defect: the v2
arm tuple was applied by mutating a module global, so a caller importing
`random_order` directly scheduled only three arms and silently skipped
`trace_min`. Arm selection is now threaded explicitly through `random_order`
and `summarize`. The aborted launch stopped after four tasks; its partial
output is preserved as `devprobe-v2-qwen-r0-aborted-3arm.jsonl`.

Requalification on three new disjoint cases (`_supersedes_ids`, `strip_ansi`,
`is_normalized_name`) failed the predeclared gate: the 7B model scored 2/3,
missing a small branch restructuring in `_supersedes_ids` despite the exact
rule in the note. By the same rule that excluded Gemma at 2/3, the 7B is
excluded from new held-out scoring. This host (15.5 GB RAM, about 1.4 GB free,
CPU-only inference) cannot load a 14B-class local coder, so a new held-out
round is blocked on hardware or qualification, not merely deferred. A
development probe of editor v2 with the `trace_min` arm on the 24
already-scored tasks (marked `dev_probe: true` throughout) is running as a
diagnostic and is never held-out evidence.

Dogfooding note: recorded agent decisions surface through `trace events`,
`trace search`, `trace context --task`, and the session-start context path
(verified: five findings returned in 39 ms with correct staleness labels after
subsequent file edits). The unbound CLI command `trace context <query>`
searches captured transcript messages only, which the development checkout
does not collect; that is documented behavior, not a recall defect.

## Next effectiveness test

Qualify the editor on disjoint repository tasks that resemble the held-out
work. Include a test-and-retry step under the same budget for every arm. On
fresh tasks, compare the ordinary TRACE block with an evidence-linked decision
excerpt and track how often TRACE can leave context empty on self-contained
requests. Preserve identical grading and compare with maintained notes in
normal agent sessions. The proposed release gate is at least a
10-percentage-point lift with paired uncertainty excluding zero and no
increase in serious regressions or disclosures.

## Verification

Windows, macOS and Linux passed [PR CI](https://github.com/PruvCode/Trace/actions/runs/43596389082)
for the code under evaluation. The installed wheel passed native Codex and
Claude Code capture and cross-agent recall in both directions using loopback
providers. Native OpenCode capture, context and evidence checks also passed.
These checks cover supported local message payloads and hook behavior.
Antigravity model-visible recall and general IDE or hosted behavior remain
unverified. The retrieval and coding trials used local models and authored
histories only.

A subsequent [Windows CI run](https://github.com/PruvCode/Trace/actions/runs/43619946981)
found a race on repeated OpenCode setup. The installer checked the monitor's
fixed temporary status path while the monitor was writing or replacing it.
Status writes now use the shared atomic writer with a private temporary file.
If the final status file stays busy, the monitor logs the failure and keeps the
last complete status. A regression test covers a legacy temporary file that
cannot be resolved.

## Follow-up on targeted recall

When an unbound request names a source file and an indexed function, TRACE
can now return an empty block when matching history is absent. It keeps the
verified decision and evidence when they exist and avoids repeating the full
decision as a definition excerpt. On the same 24 fixtures, TRACE recovered
all 21 historical decisions with evidence, returned empty context on all
three no-history controls, and reduced the median history-bearing block
from 1,077 to 820 characters.

The code and installed wheel passed [CI on all three platforms](https://github.com/PruvCode/Trace/actions/runs/43625497754).
