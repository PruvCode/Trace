# ADR: notes-first TRACE with failure-driven deeper memory

**Date:** 2026-11-16. **Branch:** `feat/executable-rules` (local, not pushed). **Status:** accepted for prototype; Goal B unvalidated.

## Inputs inspected

- **Branches and commits:**
  - `origin/main` at `eb049d4`.
  - `origin/eval/trace-maintained-notes` at `74f3096`.
  - `research/framing-probe` at `540b1bc` (local).
  - `feat/executable-rules` at `47eefac` and `bb77cde` (local).
- **Reports:**
  - `docs/engineering-audit/MAINTAINED_NOTES_AUDIT.md`.
  - Desktop: `TRACE_FRAMING_PROBE_REPORT.md`, `TRACE_HYBRID_MEMORY_PROPOSAL.md`, `TRACE_ARCHITECTURE_DECISION.md`, `TRACE_10_OF_10.md`.
  - `research/preview/repair-results/REPORT.md` (greedy and sampled runs).
  - `research/preview/graphify-results/REPORT.md`.
- **GLM:** no completed GLM maintained-notes report exists. The only GLM data is `research/phase2/run2_glm.jsonl`, from an earlier phase.

All numbers below are reproduced from regraded JSONL rows.

## Evidence that decides the design

| Context given in place of the note (24 dev tasks, Qwen 7B) | Pass | vs notes |
|---|---:|---|
| Maintained note (oracle sentence) | 11 | — |
| TRACE block, production renderer | 9–10 | loses 1–2, gains 0 |
| Decision-first TRACE block | 9 | −2, +0 |
| TRACE records reduced to `{"text": ...}` | 8 | −3 |
| Rule rendered by `memory.rules.render` (title, scope, check lines) | 10 (greedy); 30 vs 34 of 72 (sampled) | −1; −7 +2 |
| Title line + plain sentence (`clamp_limit`, 10 samples) | 5/10 | notes 8/10 |
| Graphify query | 2 | −9 |

Notes lost **0** of 120 paired comparisons in the framing probe. Every addition to the bare sentence (title, scope, record, extra history, check list) lost at least one task. No addition gained one.

**The 13 notes failures are implementation failures, not missing knowledge.** The note held the full rule in all 21 history tasks, because the benchmark gives notes the oracle. The failures are partial clauses (`iter_rows` padded but did not truncate), wrong edge cases (`clamp_limit` handled `8.5` but not `-1.1`), or broken existing behaviour. This benchmark therefore cannot show deeper memory recovering a task: nothing is missing for it to supply.

**Checks detect, the 7B model does not repair.**
- Greedy run: 26 repair calls, 0 fixes.
- Sampled run: check-driven repairs fixed 5 of 37 runs, notes-plus-tests repairs fixed 3 of 11, and none broke a passing edit.
- Wrong edits that stopped green: notes + tests 31 of 38; rules + checks 8 of 38.
- On rule-bearing tasks, checks flagged 30 of 35 wrong edits and 0 of 44 correct ones.

**The structural graph cannot resolve most cross-module calls.** I measured it on TRACE's own package code: 505 of 607 cross-module call sites are `module.func()` calls, and `code_graph.python_facts` gives every attribute call no binding (`store_mod.clamp_limit()` yields `('calls', None, None)`). At most 17% of cross-module call sites could resolve.

## A. Review of the proposed hybrid

**Rating: 5/10 as specified.** The goals are right. The control flow puts its decision points where there is no signal to decide on.

Five biggest flaws:

1. **Parity cannot come from adding to notes.** Goal A ("preserve the 11") is only reliable if the default context *is* the note. Any wrapper, title, scope label or extra record is a different prompt, and every one we tested cost a task. Parity has to be by construction (identical text), not by careful framing.
2. **The "sufficient context? / missing information?" branch has no signal.** Before an attempt, neither the agent nor a router knows what it does not know. A failed check is the first reliable signal. The trigger for deeper memory must be failure, not self-assessed uncertainty. That also removes the need for any router.
3. **Failure-driven memory is aimed at the rarer failure class.** In the evidence, failures are implementation errors (13/13), and memory cannot fix those. Feedback and model capability can. Deeper memory can only help on tasks where the needed knowledge is *not* in the notes (category 3 in §13 of the brief). The current benchmark has none of those tasks.
4. **Approval is weaker than the diagram implies.**
   - With no `origin/HEAD`, approval falls back to the local `main` branch, and any local commit, including one made by an agent with a shell, counts as approval.
   - With a remote, approval is only as strong as branch protection, which TRACE cannot see.
   - This is the same trust level as `CLAUDE.md`, and should be described that way, not as verified review.
5. **Structural impact retrieval sits on an index that sees under a fifth of cross-module calls.** Adding graph-assisted dependency checks to the default flow would mostly return nothing, or worse, a confident incomplete caller list.

Also rejected:
- **Clause-to-check mapping as a new format.** A rule bullet is already the unit that owns checks. Writing one bullet per clause gives clause-level coverage without a requirements layer.

**Is "retain every notes success and recover failures" achievable?**

Retention is achievable by construction, under three conditions:
1. The default context is byte-identical to the note.
2. Deeper memory and repair are triggered only by a failing visible check.
3. Visible checks do not false-alarm on correct code.

Under those conditions TRACE cannot alter a candidate that notes would have accepted. Condition 3 held in both trials (0 false alarms on 44 correct edits), but it depends on check quality.

Recovery is achievable only where one of these holds:
- The checks catch the failure and the agent can repair from the feedback. That needs a capable model.
- The missing knowledge exists in TRACE and not in the notes. That needs tasks of that type.

Neither has been tested. That is the open experiment.

## B. Recommended architecture

```mermaid
flowchart TD
    P[User request] --> S{Approved rule in .trace/RULES.md\nwhose file or function the request names?}
    S -- yes --> N[Plain note: the approved sentence(s),\nexactly as a notes file gives them]
    S -- no --> B[Existing TRACE recall block\n(unchanged)]
    N --> A[Agent edits]
    B --> A
    A --> C[trace rules check\n(agent, hook or CI)]
    C -- all pass --> D[Done; CI gate green]
    C -- fail --> K[Classify: flaky / environment /\nregression_in_progress / stale_or_regression\n+ repeated identical file?]
    K --> V[Deterministic next step + pointers:\nrule provenance, commits since approval,\nrepeat warning]
    V --> R{Agent chooses}
    R -- repair --> A
    R -- needs history --> M[MCP pull, on demand:\ntask_context, expand_evidence,\nevents, code_graph, get_rules]
    M --> A
    R -- budget spent / repeated --> H[Stop; human]

    subgraph Stored
      RULES[(.trace/RULES.md in git\n= approval tier)]
      DB[(memory.db: episodic events,\ntranscripts, git events)]
      G[(code graph index\nrebuildable)]
    end
    subgraph Derived per request
      N
      K
    end
    subgraph On demand only
      M
    end
```

- **Stored:**
  - Rules: git, the only instruction tier.
  - Episodic, transcript and git events: `memory.db`, unchanged.
  - Code graph: a derived index, rebuildable.
- **Derived per request:** the note view (scoped approved rules), check results and their classification.
- **On demand only:** all deeper memory, through the existing MCP tools and CLI.
- **Never injected:**
  - Proposed rules: working tree only.
  - Agent-recorded claims.
  - Transcript text as an instruction.

## C. Gap analysis

| Area | Exists | Missing / debt |
|---|---|---|
| Approved rules | `memory/rules.py`: parse, approval by default-branch ref, proposed vs approved, scoped render; `trace rules list` | Session start never delivered rules. `render` adds title, scope and check lines, which the evidence says cost passes |
| Checks | `memory/rule_checks.py`: run approved checks, rerun once, four-way classification; `trace rules check` exits 1 on blocking failures | No repeated-patch signal; no next step; no Git pointer; uncovered rules not reported by `check` |
| Session start | `session_context.get_session_start_context` → `synthetic.render_user_block`, used by native hooks (`dialogue.recall`), OpenCode (`message_context`), `trace context` (`project.preview_context`) | No notes tier; history and approved rules would be duplicated if both were injected |
| Deeper memory | MCP `task_context`, `expand_evidence`, event search, code graph, `get_rules`; CLI equivalents | None needed for the prototype |
| Trust | Synthetic label and boundary for recall; MCP `record_event` refuses `verified` and non-agent sources | Approval-ref strength not surfaced to the user |
| Graph | AST facts, import resolution, `from x import f` call resolution | `module.func()` calls never resolved (83% of cross-module call sites in TRACE) |

## D. Decisions

| Proposal | Decision | Why |
|---|---|---|
| `.trace/RULES.md`, approval = rule on default-branch ref | **Keep** | Reviewable, versioned, no new database |
| Warn when approval comes from a local branch | **Add** | A local commit is not authorization; say so instead of implying review |
| Session start delivers approved rules | **Add, flagged** (`TRACE_NOTES_FIRST=0` restores old output) | Notes parity by construction |
| Render approved rules as the bare sentence, no title, scope or checks | **Add** | Every wrapper tested lost a task. Scope and checks stay in `trace rules list` / `get_rules` |
| Suppress the recall block when an approved rule matched | **Add** | Avoids duplication. History never added a pass over notes on the dev tasks. It stays one MCP call away |
| Keep the recall block when no rule matches | **Keep** | Projects without rules behave exactly as before |
| LLM router / "sufficient context?" self-assessment | **Reject** | No signal before an attempt. A failed check is the trigger |
| Failure-driven retrieval | **Add, minimal**: deterministic next step and pointers (provenance, commits touching the file since approval, repeat warning). **Postpone** automatic injection of history | Injection is unproven and costs parity. Pointers cost nothing on the success path |
| Repeated identical patch detection | **Add** (file digest of the last failing check) | 8 of 13 greedy repair runs resubmitted identical code |
| Clause-level coverage | **Modify**: one rule bullet per clause (already supported); `check` reports approved rules in scope with no checks. **Reject** a clause-mapping format | Same coverage, no new format |
| AI-generated checks | **Reject as approved by default** | Checks become authoritative only by the same git approval as rules |
| Notes-only fallback that compares two candidates | **Postpone** | Doubles inference. Picking needs independent checks we would already be running |
| Graph module-attribute call resolution | **Modify** (fix in `python_facts`) | Root cause of the 17% ceiling; small and testable |
| Graph-assisted impact retrieval in the default flow | **Postpone** | Re-measure resolution after the fix; then test against plain source inspection |
| Episodic, transcript and git memory | **Keep unchanged** | Not exercised by the benchmark; available on demand |
| Auto-proposing note updates from verified outcomes | **Postpone** | Proposals must stay human-written until capture quality is measured |
| Run approved `py:` checks in WASI | **Reject for the product** | Approved checks are reviewed code like the test suite CI already runs; documented as such |
| Graphify integration | **Reject** | 0/21 rules delivered, 2/24 passes |

## E. Technical design

**`memory/rules.py`**
- `mentioned(rules, text) -> list[Rule]`:
  - Symbol-scoped rules match when their function name appears as a word in the request, and their file appears in it or no rule file does.
  - File-scoped rules match when their file appears.
  - If no function name matched, every rule of a named file applies.
  - File order is preserved.
- `render_notes(rules) -> str`: one rule gives the bare sentence; several give `- ` bullets. Text passes through the same redaction and invisible-character escaping as `render`.
- `load()` warns when the approval ref is a local branch.

**`memory/session_context.py`**
- `approved_notes(project_root, query) -> str`: fail-open, cheap gate on the working-tree `.trace/RULES.md`, disabled by `TRACE_NOTES_FIRST=0`.
- `get_session_start_context(..., notes_first=False)`: when true and notes match, returns `{"items": [], "text": notes, "notes": notes}` without querying the database.
- The three automatic-injection callers pass `notes_first=True` and render `result["notes"]` when present. Pull paths (MCP `task_context`, CLI) are unchanged, so deeper memory remains available.

**`memory/rule_checks.py`**
- `CheckResult.digest`: SHA-256 of the governed file at check time.
- `next_step(result, *, repeated, changes) -> str`: a fixed table per classification.
- `changes_since(root, ref, path)`: `git log --oneline -5 <ref>..HEAD -- <path>`.

**`trace rules check`**
- Stores failing digests for `regression_in_progress` in `.agent-memory/rule-checks.json` when that directory exists.
- Reports `repeated` when the same file content fails the same check again.
- Lists approved rules in scope that have no checks.
- JSON adds `next_step`, `repeated`, `changes_since_approval` and `uncovered`.

**`memory/code_graph.py`:** an attribute call on a name bound by `from pkg import module [as alias]` becomes a call observation for `pkg.module` with the attribute as its symbol. It resolves only if that file exists and exports exactly one such definition, so no false links are created.

**Migration:**
- No schema change.
- Rules files are optional.
- With no rules, or `TRACE_NOTES_FIRST=0`, session-start output is byte-identical to before.

## F. What was implemented

Local, uncommitted, on `feat/executable-rules`. Every change is reversible and flagged:

- **`memory/rules.py`:**
  - `mentioned()`: function rules match only when their name appears in the request; file rules match on their path.
  - `render_notes()`: the bare sentence or sentences.
  - A local-branch approval warning.
- **`memory/session_context.py`:**
  - `approved_notes()`.
  - `get_session_start_context(notes_first=)`.
- **Automatic-injection callers:** `memory/dialogue.py` (native hooks), `memory/message_context.py` (OpenCode) and `trace_memory/project.py` (`trace preview`) pass `notes_first=True`. Pull paths are unchanged.
- **`memory/rule_checks.py`:** a file digest per result, `changes_since()` and `next_step()`.
- **`trace rules check`:** a repeat warning (`.agent-memory/rule-checks.json`), `next:` lines, commits since approval, and rules with no checks.
- **`memory/code_graph.py`:** `alias.func()` resolves when `alias` is a module imported with `from pkg import module`.
- **Not built (postponed or rejected in D):** a router, automatic history injection after failure, clause mapping, the notes-only fallback, graph retrieval in the default flow.

A scoping flaw in the first draft of `mentioned()` was found while designing the parity run. If a request named a function that had no rule, every rule in that file was injected. Function rules now match only by name. That is covered by a test and by the three no-history controls below.

## G. Tests and verification

| Command | Result |
|---|---|
| `pytest tests/memory/test_notes_first.py tests/memory/test_rules.py tests/cli/test_rules_cli.py tests/memory/test_entity_graph.py` | 41 passed (28 after the scoping fix, rules and notes files) |
| `pytest` (full suite, first run, before the scoping fix) | exit 0, no failures (731 collected; 23 skipped and 1 xfailed counted from progress output) |
| `pytest` (full suite, final code, under CPU load from the model run) | 706 passed, **1 failed**, 23 skipped, 1 xfailed |
| `pytest tests/memory/test_mcp.py::test_mcp_server_missing_db_exits_loudly` (alone) | passed |

The one failure was a subprocess `TimeoutExpired` at 60 s in an MCP startup test, while Ollama was saturating the CPU. Alone it passes in about 60 s, right at its timeout. That makes it load-sensitive, not caused by this change. The test is left unchanged.

**Graph resolution, re-measured** on TRACE's own `memory/` and `trace_memory/`, indexed with the old and new extractor: resolved Python call edges went from 724 to 1,179, and **resolved cross-file call edges from 79 to 534**. A syntactic count found 607 cross-module call sites, so coverage is now about 88%, up from about 13%. Function-local imports are still unresolved by design.

## H. Evaluation (development tasks only)

`research/preview/notes_first_trial.py`, results in `research/preview/notes-first-results/`:
- Qwen 2.5 Coder 7B, greedy, the frozen one-shot protocol.
- 72 runs, independent regrade 72/72 reproduced.
- The trace arm's context matches the frozen Graphify-trial contexts on all 24 tasks once random evidence IDs are masked.

| Arm | Pass | Notes successes retained | Lost | Notes failures recovered | Unresolved | Preservation failures | Median input tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| Maintained notes | 11/24 | — | — | — | 13 | 1 | 208 |
| Previous TRACE block | 9/24 | 9 | **2** (`clamp_limit`, `iter_rows`) | 0 | 13 | 2 | 439 |
| **Notes-first TRACE** (real `dialogue.recall` hook) | **11/24** | **11** | **0** | 0 | 13 | 1 | 208 |

- **Retrieval:** notes-first injected exactly the approved sentence on 21/21 rule tasks and nothing on the 3 controls. Its context was byte-identical to the note on 24/24 tasks.
- **Verification overhead:** none on the injection path. Input tokens matched notes, and the old block's median of 439 dropped to 208.
- **Nondeterminism:** on `_text`, identical prompts produced different candidates in the same batch. Both failed, so no outcome changed, but this is why parity must be argued by construction and measured, not assumed from one run.
- **Retrieval contribution to coding:** none measurable here. History injection lost 2 tasks; notes-first injects no history.

Fresh evidence: **none.** The capable-agent experiment on fresh tasks has not been run. See `GUARDRAILS.md` for the next gate.

## I. Documentation

`README.md`, new section "Project rules (approved engineering notes)", covers:
- the rules format;
- what approval means and how weak it is without branch protection;
- what the agent sees and when deeper memory is used;
- what checks verify and how failures are classified;
- `next:`, repeat and coverage output;
- inspection commands;
- `TRACE_NOTES_FIRST=0`.

## J. Verdict

1. **Did the architecture improve TRACE?** On the development tasks, yes, in one specific way. TRACE no longer loses tasks to notes: 11/24 against 9/24 for the old block in the same batch, with half the input tokens. It does not yet beat notes. Recovered: 0.
2. **Proven:**
   - Notes parity by construction through the real hook, on development tasks.
   - Checks detect rule violations, with no false alarms on correct edits, from the earlier repair trial.
   - The graph's cross-module call coverage was fixed.
3. **Hypothetical:**
   - Recovery of notes failures, through repair with a capable model or through knowledge missing from notes.
   - Whether a capable agent pulls on-demand memory usefully.
   - Correction-to-rule generation.
   - Recurrence reduction.
4. **Largest contributor:** removing everything except the approved sentence from the default context.
5. **Remove or simplify:**
   - Routine history injection when a rule matches (done).
   - Graph and transcript output in prompts.
   - Graphify.
   - The router and self-assessment step from the proposed flow.
6. **Build next:** the correction-to-rule gate in `GUARDRAILS.md`, preregistered, about $32–45 in API cost, capped at $100, and only with approval.
7. **Implementation rating (evidence only): 6.5/10**, up from 6.0. Parity is now demonstrated, the graph defect is fixed, and approval strength is surfaced. Nothing yet shows TRACE beating notes.
8. **Ideation rating:** **7/10** for this notes-first architecture on its own. For the guardrails direction it serves, my rating is 8.5 and the external review's is 9.3; both depend on the gate.

## K. Stabilization and safety audit (2026-11-16, before commit)

**Full suite with no concurrent model jobs:** 707 passed, 23 skipped, 1 xfailed, 0 failed, in 12 min.

**MCP startup timeout, isolated:** `test_mcp_server_missing_db_exits_loudly` was not among the 15 slowest tests in the quiet run (all of those took 10.7 s or more), and a manual run exits in about 5 s. Its earlier 60 s `TimeoutExpired` happened only while Ollama saturated the CPU. Left unchanged; run the suite without model jobs.

**Defects found and fixed during the audit:**

| Defect | Effect | Fix |
|---|---|---|
| `py:` checks loaded the governed file as a standalone module | Any package module with relative imports (e.g. Click's `src/click/core.py`) errored and was classified `environment`, so the check silently never ran | Inside a package, import by dotted name with the package root first on `sys.path`; otherwise load the file as before |
| Check subprocesses dropped `PYTHONPATH` | A src-layout project's tests imported the installed copy, not the checked tree | `subprocess_executor(..., pythonpath=("src",))` |
| A governed or test path could resolve outside the repository through a link | A reviewed rule could still execute a file outside the tree | Refused as `error`; tested with a directory junction on Windows |

**Approval and execution properties, each tested:**

| Property | Test |
|---|---|
| Proposed rules are never executed | `test_proposed_checks_are_never_executed` |
| A working-tree edit to an *approved* rule's check is not executed; the approved version runs | `test_a_working_tree_edit_to_an_approved_check_is_never_executed` |
| Proposed rules are never injected | `test_proposed_rules_are_never_injected` |
| Injected text comes from the approval ref, not the working tree | `test_injected_text_comes_from_the_approval_ref_not_the_working_tree` |
| No repository means nothing is approved | `test_no_rules_file_or_no_git_is_silent` |
| Local-branch approval is flagged | `test_local_branch_approval_is_flagged` |

**Not fixable locally, documented in the README:** `origin/HEAD` and `origin/main` are local refs that any process with a shell can rewrite. On a developer machine, approved rules therefore have the same trust as `CLAUDE.md`. The trust anchor is `trace rules check --all` in CI on a fresh checkout of the protected branch.
