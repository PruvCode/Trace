# TRACE final engineering and product audit (2026-10-31)

## Verdict

**READY FOR EXPERIMENTAL RELEASE**, with `main` unchanged until the open gates below close. Rating **6.8/10**.
TRACE is now a sound, secure-by-default local memory layer that tied best-case notes on correctness at lower cost in a
small test. It has not shown it is better than notes on correctness.

## Rating

| Category | Weight | Before (5.9 audit) | Now | Evidence |
|---|---:|---:|---:|---|
| Architecture | 15% | 6.5 | 7.0 | Two OpenCode generations on one store; still depends on OpenCode's private database |
| Code quality | 10% | 6.5 | 6.5 | Linted, one redaction module; dead legacy monitor remains |
| Reliability | 15% | 6.0 | 7.5 | 586 tests, CI on Windows and Linux; live suite not rerun |
| Memory capture | 15% | 7.0 | 7.5 | Capture failure modes from live runs and review closed; 6/6 stores held the fact |
| Retrieval usefulness | 15% | 4.5 | 6.0 | 12/12 held-out; tied notes live on a small sample |
| Security | 5% | 6.0 | 7.0 | Forging closed, redaction broadened; known limits remain |
| Compatibility | 10% | 4.0 | 6.5 | 2.2.5 and 2.1.4 work standalone; service mode and Claude Code do not count |
| Performance | 5% | n/a | 7.0 | Block halved, per-project install gone |
| Developer experience | 5% | 6.0 | 6.5 | Clean install, teardown; source install only |
| Research confidence | 5% | 5.5 | 6.0 | Preregistered held-out work, cut short by the usage limit |

Weighted total: **6.8**. The rise from 5.7–5.9 comes from fixes that were tested, not from a demonstrated win over notes.

## What was fixed

- **Stored text can no longer pose as the memory block.** An independent reviewer tried about 1.1M code points across all fields.
- **Redaction** moved to one module with bounded patterns, runs on every write and output, and handles far more formats.
- **The "100-second exit delay" was a one-time `npm install` at startup.** It now happens once per user, not per project.
- **OpenCode 2.x works.** TRACE did nothing there before.
- **Context cost:** the block fell from 4,222 to 1,890 characters in live runs, and default MCP tools from nine to four.
- **Capture bugs found by live runs and review:** path aliases, stdlib shadowing, malformed Unicode, long replies, deleted
  rowids, an upgrade under a running monitor, bad tool arguments, and a Linux zombie process.
- **CI** now tests a clean checkout on Windows and Linux.

## Test results

| Command | Result |
|---|---|
| `python -m pytest tests -q -rfE`, fresh default Windows clone at `575ace3`, `pyarrow` installed | 586 passed, 0 failed, 22 skipped, 1 xfailed, 520 s |
| GitHub Actions, windows-latest and ubuntu-latest, at `e0af16d` | Passed |
| `compat_probe.py` on 2.2.5, 2.1.4 (`--standalone`), 1.18.32 | Recall, capture, injection, MCP all passed |

Skips: 21 live OpenCode tests and one symlink test on Windows. **The 21 live tests were not executed**: the model hit its
usage limit, and they must not be counted as passing. Commits after `575ace3` changed docs, research code and four
redaction rules. CI covers the final commit.

## Performance

| Measure | Before | After |
|---|---|---|
| New project, first run on 1.18.32 with a primed user | 86 s | 15 s |
| Repeat run | 13–17 s | Unchanged |
| OpenCode 2.x first run | TRACE did not work | 8 s |
| Median block in live runs | 4,222 chars | 1,890 chars |
| Median input tokens, all completed runs | 76,119 | 68,717 (−10%) |
| Held-out retrieval | 10/12 at 3,010 chars | 12/12 at 1,490 chars |

## Comparative research

Phase 3 on OpenCode 2.2.5, `big-pickle`. Six held-out tasks, five-session histories, notes given their best case.
The usage limit ended the run: **31 of 72 rows ran, covering four tasks; one was cut off and is void**. The rest of the
data, including tasks h5 and h6 and every no-notes row, is unusable.

| Arm | Pass | Median input tokens | Median seconds | Left workspace |
|---|---:|---:|---:|---:|
| No memory | 6/8 | 205,425 | 70 | 7 of 8 |
| Notes, best case | 8/8 | 79,829 | 44 | 3 of 8 |
| Previous TRACE | 7/7 | 76,119 | 37 | 3 of 7 |
| Current TRACE | 7/7 | 68,717 | 39 | 3 of 7 |

- **TRACE did not beat notes on correctness:** tied, with too little data to separate them.
- **Cost:** current TRACE used 14% fewer tokens than notes here, reversing Phase 2's roughly double. Different version and
  configuration, so this is not a like-for-like change.
- **No-memory passes were mostly not real:** 7 of 8 runs searched outside the workspace. The one that stayed failed.
- **Task-aware retrieval did not change correctness live.** Previous TRACE recovered facts through MCP tools in 7 of 7 runs,
  so the tools matter more than earlier data suggested.
- **The main untested advantage is capture when nobody asks for notes.** That secondary arm is **BLOCKED**.
- Across Phase 2 and earlier runs: facts reached the store more often than notes, 18/20 vs 15/20.

Hypotheses: H1 and H2 direction supported, contaminated. H3 not supported. H4 tied. H5 supported on a small sample. H6 BLOCKED.

Limits: synthetic tasks, tiny fixtures, one free model, same author for tasks and product, prompts hint a decision exists.
Phase 2 had the same disk exposure as the first Phase 3 smoke run, so its no-memory figures may be affected.

## Security

Resolved: stored-text forging, unredacted write paths, quadratic regexes, secrets in status files and logs, `-P` isolation.

Remaining:
- A model can still choose to obey text inside a record. Framing limits this; it cannot remove it.
- 27 of the reviewer's secret shapes still pass, by design where catching them would corrupt ordinary text, including
  `token=abcdefghij`, bare random strings, and passwords in prose. 18 ordinary strings in that test are over-redacted.
- Memory is stored as plain text. `trace report` and `trace context` do not re-redact rows stored by older versions.
- The first prompt is visible in process listings during the helper's run.

## GitHub

Repository `PruvCode/Trace`, branch `release/trace-current-audit`, [draft PR #3](https://github.com/PruvCode/Trace/pull/3).
`main` is unchanged at `eb467f89`. See `GITHUB_SYNC.md`.

## Remaining issues

**Critical blockers:** none known.

**Merge gates not met:** 21 live tests unrun, no-notes comparison unrun, no human approval.

**Noncritical:** subagent sessions created before first capture can reach memory; memory-tool activity is not captured on
2.x; legacy monitor and duplicate config classes remain; the PR description is stale.

**Unverified:** 2.x background-service mode, Linux or macOS with a real agent, large real histories, stronger models.

**Future:** Claude Code through its documented hooks, more languages for structural indexing.

## Recommendation

TRACE is a serious, reliable enough open-source engineering tool to put in front of experimental users. It is not yet a
proven improvement on a good notes file. Position it narrowly as **automatic notes**: memory that keeps working when no
one asks an agent to write anything down. Finish the blocked no-notes test next. If TRACE wins there, that is the product.
If not, simplify it to automatic capture plus search.
