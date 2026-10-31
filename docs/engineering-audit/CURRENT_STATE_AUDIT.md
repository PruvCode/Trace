# TRACE current-state audit (2026-10-30)

Superseded in part by `FINAL_REPORT.md`: SEC-1, SEC-2, PERF-1, COMPAT-1 and ENG-1 below have since been addressed.
PERF-1 in particular was misdiagnosed here as an exit delay; it was a startup install.

Auditor: Claude Opus 5.5. Audited branch: `release/trace-current-audit`, built on `phase/2-eval`.
This records what was run and inspected, not what earlier ledgers claimed. Earlier claims that did
not hold are corrected in section 7.

## 1. Verdict

**Research prototype.** Suitable for invited testers pinned to OpenCode 1.18.32. Not ready for
general release: the OpenCode 2.x that users now run is untested, and on the evidence so far TRACE
is no better than a notes file while costing more.

## 2. Rating: 5.9 / 10

| Category | Weight | Score | Basis |
|---|---:|---:|---|
| Architecture and design | 15% | 6.5 | Clean local layering and project isolation; depends on an experimental hook and on OpenCode's private DB schema; two parallel capture adapters |
| Code quality | 15% | 6.5 | 94% of functions return-annotated, no bare `except`, no TODOs; 1036-line service, duplicated classes, no linter, type checker or CI |
| Reliability and testing | 15% | 6.0 | 439 passing on a fresh clone; two clean-clone defects survived until this audit; live suite not run; unexplained exit delay |
| Memory capture and persistence | 15% | 7.0 | Key facts stored in 18/20 real sessions; idempotent capture; tool output deliberately not captured |
| Retrieval and practical usefulness | 15% | 4.5 | Large gain over no memory, none over a notes file; about 2x tokens; tiny stores only |
| Agent integration and compatibility | 10% | 4.0 | Automatic path only on 1.18.32; no Claude Code integration |
| Security and privacy | 5% | 6.0 | Parameterized SQL, no shell, exact project match; memory block can be forged, redaction has gaps |
| Developer experience | 5% | 6.0 | Clean install and CLI quick-start work; source install only |
| Research and benchmark evidence | 5% | 5.5 | Preregistered, repeated, honest; synthetic, tiny, one model |

Weighted: 4.58 (first five) + 0.40 + 0.88 = **5.85**. Engineering quality (about 6.5) is ahead of
demonstrated product value (about 4.5); the overall number should not hide that gap.

## 3. What is implemented

| Capability | State | Evidence |
|---|---|---|
| Structural memory (tree-sitter, Python only) | Implemented | Tests; CLI smoke |
| Episodic memory (events, status, supersedes, file staleness) | Implemented, unused automatically | 0 events recorded by agents in run 1, 1 in run 2 |
| Git memory | Implemented | Tests; used once in 160 Session B runs |
| Transcript capture | Implemented, carries the automatic path | Real runs |
| Automatic injection on every request | Implemented on 1.18.32 | Injected on every request in 58/58 completed C2/C3 runs |
| Task-aware retrieval (C3) | Implemented, on by default, no shown benefit | `TRACE_TASK_AWARE=0` disables it |
| MCP server (9 tools) | Implemented, rarely used | 0/40 C2/C3 runs called one in run 1, 6/40 in run 2 |
| Tool output capture | Deliberately absent | README; failed commands survive only if an agent describes them |
| Staleness labels | Findings only | Transcript text, the main carrier, gets no staleness label |

In the automatic path TRACE is, in practice, a tail of the last conversation plus keyword matches.
The other three layers did not contribute to the measured results.

## 4. Testing

Environment: Windows 11, Python 3.11.15, fresh venv from `pip install -e ".[dev]"`, fresh
`git clone` with the default `core.autocrlf=true`.

| # | Command | Tree | Result |
|---|---|---|---|
| A1 | `python -m pytest tests -q` | `release` worktree at `12e245e` (phase/2-eval, CRLF checkout) | **28 failed**, 411 passed, 29 skipped, 1 xfailed, 378 s |
| A2 | same, `-rs` | fresh clone at `a1e4b09` (adds `.gitattributes`) | **1 failed**, 438 passed, 29 skipped, 1 xfailed, 506 s |
| A3 | same, `-rs` | fresh clone at `2489795` (adds fixture dependency) | **439 passed, 0 failed, 29 skipped, 1 xfailed**, 601 s |
| A4 | `python -m pytest tests/corpus -q` with `pyarrow` installed | fresh clone at `2489795` | 80 passed, 50 s |

Skips in A3: 21 live OpenCode tests and 8 `pyarrow` tests (the 8 ran in A4). The xfail is a strict
known limitation in `test_long_session_reliability.py`. No test was weakened; the only test edit adds
a missing fixture dependency.

**Not executed:** the 21 live tests. `TRACE_LIVE_OPENCODE=1` runs need a model, and the provider now
returns `Free usage limit reached`. A plain `opencode run` with no TRACE installed fails the same
way, so this is not a TRACE fault. Paying is not authorized. Live evidence for this runtime code is
Phase 2 run 1, which ran at `4363ff4`: `memory/`, `trace_memory/`, `agent/`, `benchmark/` and
`corpus/` are byte-identical to the audited tree.

Flaky or environmental: older reports describe `WinError 32` teardown flakes in live tests. They were
not re-tested here.

## 5. Injection policy

Per-request injection is the right mechanism on 1.18.x. OpenCode does not persist the plugin's
message, so once-per-session loses memory after the first request: GLM's probe saw 1/9 and 1/8
requests injected, against 5/5 for per-request, with no duplicates. Whether that improves task
outcomes is unmeasured. Its cost is real but not separated from other TRACE costs.

## 6. Phase 2 results

Design: `docs/autopilot/PHASE2_PREREGISTRATION.md`. Two complete runs of 80 Session B runs each, on
OpenCode 1.18.32 with `opencode/big-pickle`. Full numbers: `docs/autopilot/RESEARCH_EVIDENCE.md`.

| C_episodic pass rate | C0 none | C1 notes | C2 TRACE | C3 task-aware |
|---|---:|---:|---:|---:|
| Run 1 (n=12 per arm) | 50% | 92% | 83% | 92% |
| Run 2 (n=12 per arm) | 25% | 75% | 83% | 75% |
| Pooled, exploratory (n=24) | 38% | 83% | 83% | 83% |

- Memory beats no memory: +46 pp pooled for each arm, all CIs exclude zero.
- TRACE does not beat a notes file: C2−C1 0 pp (CI −25 to +25), C3−C1 0 pp (CI −21 to +17).
  This is inconclusive, not proof of equality, but the product hypothesis H5 is **not supported**.
- Task-aware retrieval does not beat recency: C3−C2 0 pp (CI −13 to +13). H3 is not supported.
- Post-hoc, dropping 33 rows with zero tool calls that timed out, most likely from the free-tier limit: C1 83%, C2 87%, C3 95%; C3−C1 +12 pp
  (CI −9 to +33). Still inconclusive.
- Cost, completed C runs only: median input tokens 46k for C1, 109k for C2, 75k for C3. Median wall
  time 33 s for C1, 107 s for C2, 120 s for C3.
- Harm (H4): run 1 passed 8/8 in every arm. Run 2's D tasks are void. **H4 is weakly tested**: two of
  the four D prompts tell the agent to check current source, which protects every arm.

Limits: synthetic tasks, Session A stores of 2–8 messages (so the last-10 tail always contains the
fact), one free model, same author for tasks and product. These results say nothing about retrieval
from a large, real store.

## 7. Corrections to earlier ledgers

| Claim | Where | Finding |
|---|---|---|
| Clean-checkout suite green, 439 passed | GLM T8 | Not reproducible from a default Windows clone: 28 failed (A1). GLM's clone had LF fixtures. Fixed by `.gitattributes` and one fixture dependency |
| DEF-1 fixed | GLM KNOWN_ISSUES | Partly. See above |
| `run1.jsonl` is partial, "a few B-phase conditions" | `research/phase2/README.md` | It is complete: all 80 Session B runs |
| Phase 2 full evaluation NOT RUN | GLM STATUS | Two complete runs exist. GLM's run was never committed; it is now `research/phase2/run2_glm.jsonl` |
| Opening capture stores overreached permissions | GLM-D4 | Directive section 1.2 item 5 allowed those stores after ownership checks. The access was read-only and count-only |

## 8. Findings

| ID | Severity | Finding |
|---|---|---|
| SEC-1 | High | **Memory block can be forged.** Stored text is injected raw, with newlines. A stored message containing the footer `(End of recovered memory...)` followed by `SYSTEM: ...` appears unescaped in the block, then on every request of later sessions. Verified with a local probe. Any hostile content an agent repeats can persist this way |
| PROD-1 | High | No shown advantage over a free notes file, at about 2x tokens |
| COMPAT-1 | High | OpenCode 2.x untested. On this machine it runs as a background `serve --service` daemon, so XDG isolation may not protect the owner's real history. Testing needs a disposable environment |
| PERF-1 | Medium | `opencode run` exits about 100 s late with TRACE: harness wall time 149 s against 47 s of agent activity for C2, 34 s against 32 s for C1. Cause not found; the planned isolation experiment was blocked by the usage limit |
| SEC-2 | Medium | Redaction misses `token: tok_...`, `the service credential is cred_...`, `curl -u admin:pw`, `DATABASE_URL=sqlite://u:p@h/db` and SSNs. Stored as plain text |
| ENG-1 | Medium | No CI, no lock file, no linter or type checker. Both clean-clone defects would have been caught by CI |
| ENG-2 | Medium | Parallel adapters: `opencode_capture.py` keeps its own weaker `_sanitize_content`; `CaptureConfig` and `OpenCodeSchemaError` are each defined twice |
| ENG-3 | Low | SQLite uses rollback journaling (no WAL) with a 5 s busy timeout |
| SEC-3 | Low | The user's first prompt, up to 2000 characters, is passed on the helper's command line, so it is visible in process listings |
| RET-1 | Low | `trace context` searches transcripts only: a recorded finding about `get_session_timeout` was not returned for "timeout" |

Not found: hardcoded credentials, SQL injection (f-string SQL interpolates only constants), `shell=True`,
unbounded deletion (`trace reset` needs `--yes`), cross-project capture (exact directory match). The
PID kill path requires proof that the process is TRACE's monitor. Secret scans of the branch found
only deliberate test strings.

## 9. Publication notes

- Published tree excludes `.env`, `.agent-memory`, OpenCode databases, transcripts, venvs and caches.
- Tracked research files are content-free: tool counts, pass/fail rows and labels. They contain the
  local username in temp paths, as files on `main` already do.
- `fixtures/creds_repo` holds fake values only.
