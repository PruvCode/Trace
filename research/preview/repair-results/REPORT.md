# Repair trial: maintained notes + tests vs TRACE rules + executable checks

**Status:** complete. Greedy run (1 seed) and sampled replication (3 seeds, temperature 0.8) both independently regraded with 0 mismatches.
**Label:** development pilot on the 24 previously seen maintained-notes tasks. Not held-out evidence.
**Code:** `feat/executable-rules` at `bb77cde` (harness) on top of `47eefac` (product: `memory/rules.py`, `memory/rule_checks.py`). Model Qwen 2.5 Coder 7B (`dae161e2…`), CPU, `num_ctx` 4096, 400 output tokens, frozen fenced prompt and editor v2 extraction, WASI grader.

## Design

| Arm | Context | After each attempt the agent runs | Repairs on |
|---|---|---|---|
| `notes_repair` | the maintained note | existing tests | failing tests |
| `rules_text` | the approved rule rendered by `memory.rules` (sentences, checks listed as text) | existing tests | failing tests |
| `rules_checks` | same as `rules_text` | existing tests **and** the rule's checks via `memory.rule_checks` | either |

- Budget: at most 3 model calls (first attempt + 2 repairs). The loop stops as soon as every check the arm can run passes.
- Feedback lists each failing check with the actual value of its left side (as a test runner would). Hidden grader checks are never shown.
- `tests` (existing behavior) and rule `checks` were authored for this trial in `repair_cases.py`. Preflight asserts: no visible check repeats a hidden clause; tests pass on the original and the reference; every rule's checks pass on the reference and at least one fails on the original.
- Analysis key: (task, arm, seed, run, temperature, first-prompt SHA-256); identical duplicates collapse, conflicting duplicates abort.

## Greedy result (temperature 0, seed 52183)

| Arm | First-attempt pass | Final pass | Final preservation failures | Model calls | Repairs | Median latency | Median input tokens | Median output tokens |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `notes_repair` | 11/24 | **11/24** | 1 | 26 | 2 | 31 s | 211 | 96 |
| `rules_text` | 10/24 | 10/24 | 1 | 26 | 2 | 30 s | 300 | 96 |
| `rules_checks` | 10/24 | 10/24 | 1 | 50 | 26 | 59 s | 1016 | 156 |

Paired: `rules_checks` vs `notes_repair` +1 (`_version_join`) −2 (`is_commit`, `iter_rows`); `rules_checks` vs `rules_text` identical on every task. Independent regrade: 72 rows, 102 attempts, 0 mismatches. No infrastructure errors.

### What the repairs did

- **Executable checks repaired nothing.** 13 `rules_checks` runs used repairs (26 calls); none changed a failing edit into a passing one, and none broke a passing one. In 8 of the 13 the model resubmitted the identical function on every repair, even though the feedback showed the failing check and the actual value (e.g. `_posixify` added `.strip('-')` and stopped there; `iter_rows` was resubmitted unchanged twice).
- **Executable checks did detect the failures.** Runs that stopped with every runnable check green but failed the hidden grader:

| Arm | Stopped green but actually wrong |
|---|---:|
| `notes_repair` | **12** |
| `rules_text` | **13** |
| `rules_checks` | **1** (`_apply_budgets`, a task with no rule) |

  Every rule-bearing task the model got wrong was flagged by its rule checks; no correct edit was flagged (all 10 hidden passes also passed their checks).

## Interpretation (greedy, one seed)

1. **On coding success, executable memory gave no advantage over a well-written note plus tests.** It cost about twice the calls and latency and five times the median input tokens.
2. **On detection it was decisive.** With notes and existing tests, 12 of 13 wrong edits looked finished. With rule checks, 13 of 13 wrong rule-bearing edits were visibly failing. In a workflow where CI blocks a red merge (`trace rules check --all`), that is 12 non-compliant changes caught rather than merged.
3. **The bottleneck is the model's ability to use feedback**, not the memory format. The 7B one-shot editor cannot repair from a failing assertion; it mostly echoes its previous answer.
4. Caveat on (2): the checks were written by someone who could see the reference change, and chose inputs that discriminate. Real rule checks written by maintainers may be weaker; detection recall must be measured on checks written without reference access.

## Sampled replication (temperature 0.8, seeds 52183, 52189, 52193)

216 runs (24 tasks × 3 arms × 3 seeds), 317 attempts. Independent regrade: 0 mismatches. The run was paused for the Graphify trial and resumed by run key; no duplicate rows.

| Arm | First-attempt pass | Final pass | Final preservation failures | Model calls | Runs that repaired | Repairs that fixed | Repairs that broke | Median latency | Median input tokens | Median output tokens |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `notes_repair` | 31/72 | **34/72** | 6 | 91 | 11 | 3 | 0 | 23 s | 224 | 98 |
| `rules_text` | 29/72 | 30/72 | 8 | 86 | 8 | 1 | 0 | 22 s | 305 | 98 |
| `rules_checks` | 29/72 | **34/72** | 5 | 140 | 37 | 5 | 0 | 38 s | 642 | 142 |

Paired by (task, seed), distinct tasks with a gain or loss: `rules_checks` vs `notes_repair` +5 −6; `rules_checks` vs `rules_text` +4 −1; `rules_text` vs `notes_repair` +2 −7.

Detection:

| Arm | Stopped green but actually wrong |
|---|---:|
| `notes_repair` | **31** of 38 wrong |
| `rules_text` | **36** of 42 wrong |
| `rules_checks` | **8** of 38 wrong (3 on tasks with no rule) |

On rule-bearing tasks, `rules_checks` flagged 30 of 35 wrong final edits (86%) and flagged none of the correct ones.

### What the replication changes

- **Repair is not zero once sampling is on.** Checks fed into repair fixed 5 runs (notes and tests fixed 3), so `rules_checks` caught up from 29 to 34 first-attempt-to-final. It lands exactly on the notes arm (34/72), at 1.5× the calls, 1.7× the latency and about 3× the input tokens.
- **Rule framing still costs on the first attempt:** `rules_text` trails notes 29 vs 31 first try, 30 vs 34 final, the same direction as the framing probe.
- **Detection holds and is the stable finding:** 31 of 38 wrong notes-arm edits looked finished; with checks, 8 of 38 did. Recall on rule tasks drops from 13/13 (greedy) to 30/35 under sampling, with no false alarms.

## Next

- The repair question needs a model that can repair: repeat with a capable tool-using agent before drawing any conclusion about whether executable rules improve *coding*.
- The detection result argues for shipping `trace rules check` as a CI gate regardless of the coding question.
