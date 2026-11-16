# `trace rules propose` on real Click fixes (prototype demo)

Click is a TRACE development repository, so this demo is not gate evidence.

**Setup:**
- Repository `pallets/click`, full clone at `HEAD` of 2026-11-16.
- Python 3.11, the trace-ci venv's pytest, `--pythonpath src --later 3`.
- One JSON validation report per commit in this directory (re-run after the ancestry fix below).

| Fix commit | Checks validated (fail before, pass after) | Later descendants checked | Later outcome |
|---|---:|---:|---|
| `07c909f` validate style() color arguments | 1 | 3 | all pass |
| `6f85d26` no BytesWarning under `python -bb` | 1 | 3 | all pass |
| `c53d373` show_envvar output for sequence/empty envvar | 2 | 3 | all pass |
| `f58ca3e` copy/deepcopy/pickle of Sentinel members | 2 | 0 | none touch the file |
| `f67c2bb` short help abbreviation | 1 | 2 | all pass |
| `762c97e` double-bracketing of choices | 0 | — | environment: the fix's test module does not collect under this pytest with Click's `filterwarnings = error` |
| `bec5928` package_name resolution | 0 | — | same environment problem |

**Results:**
- **Yield:** 5/7 fixes produced a validated guardrail (7 checks). Both misses are environment mismatches, not evidence about the method.
- **False alarms:** 0 across 11 later descendants.

**Correction to the first version of this demo.** It reported that `c53d373`'s tests were later renamed (`missing_check` on 3 revisions). That was wrong. The validator listed later commits with `rev-list fix..HEAD`, which includes commits from branches that forked before the fix and were merged later. Those commits never contained the fix or its tests. The validator now uses `--ancestry-path` (tested), and every later descendant passes.

The `missing_check` classification itself remains: a renamed or removed test does silently disarm a guardrail. But this demo does not show it happening.

**Not shown:**
- Relevance. Rule sentences are commit subjects (e.g. "Fix `show_envvar` output for ..."), which describe the change rather than the requirement; review must reword them.
- Generation for fixes that came without tests.
