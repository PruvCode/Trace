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
