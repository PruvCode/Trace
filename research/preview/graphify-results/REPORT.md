# Graphify vs TRACE vs maintained notes on the 24 frozen tasks

**Status:** greedy run complete and independently regraded (120/120 reproduced, 0 mismatches).
**Label:** development probe on the 24 previously seen maintained-notes tasks. Not held-out evidence. One seed at temperature 0.
**Setup:** Qwen 2.5 Coder 7B (`dae161e2…`) on CPU via Ollama, `num_ctx` 2048, 400 output tokens, the frozen fenced prompt with editor v2 extraction and one format repair (`notes_trial.proposal`), WASI grader. Harness: `research/preview/graphify_trial.py`. Graphify `graphifyy` 0.9.111 in `C:\Users\pruth\graphify-eval\.venv`.

## Arms

| Arm | Context given to the model |
|---|---|
| `no_memory` | nothing |
| `notes` | the maintained note (oracle: the exact rule for the task) |
| `trace` | TRACE seeded with the same note, rendered by the production renderer (frozen recipe) |
| `graphify_code` | `graphify query "<request> File: <path>" --budget 750` over a code-only graph of the pinned repo (tree-sitter AST, no LLM) |
| `graphify_history` | same query over a graph of the repo plus `decisions.md` (the same 21 rules as the notes) and `sessions.md`, semantically extracted with Ollama qwen 7B |

## Retrieval (before any coding)

| | rule present in context | median context chars |
|---|---:|---:|
| `notes` | 21/21 | 167 |
| `trace` | **21/21** | 1,046 |
| `graphify_code` | 0/21 | 3,002 |
| `graphify_history` | **0/21** | 3,008 |

Graphify query output is a BFS over node labels and edges, truncated to the budget (e.g. `iter_rows`: "showing 28 of 242 nodes"). It never carries the text of a decision, only labels. Semantic extraction of the docs by qwen 7B produced 2 doc nodes per repo ("Engineering decisions", "Session transcripts"), so the individual rules never became retrievable nodes.

## Coding result

| Arm | Pass | History tasks (21) | Controls (3) | Preservation failures | Median input tokens | Median latency |
|---|---:|---:|---:|---:|---:|---:|
| `no_memory` | 2/24 | 0/21 | 2/3 | 0 | 182 | 20 s |
| `notes` | **11/24** | **9/21** | 2/3 | 1 | 208 | 24 s |
| `trace` | 10/24 | 8/21 | 2/3 | 1 | 442 | 36 s |
| `graphify_code` | 2/24 | 0/21 | 2/3 | 2 | 1,078 | 69 s |
| `graphify_history` | 2/24 | 0/21 | 2/3 | 1 | 1,090 | 69 s |

All 120 edits were valid (well-formed replacements).

Paired comparisons:
- `trace` vs `no_memory`: +8, −0.
- `trace` vs `notes`: +0, −1 (`clamp_limit`, the same record-framing loss as in the framing probe).
- `graphify_*` vs `no_memory`: +0, −0. Graphify gave the same outcome as no memory on every task.
- `graphify_*` vs `trace`: +0, −8.

Graphify preservation failures: `is_git_path` (code) and `is_valid_pylock_path` (both arms). In both cases the model broke existing behavior, while no-memory edits of the same tasks broke nothing.

## Interpretation

1. **For this benchmark, Graphify is not a memory layer.** These tasks need a project-specific rule, and Graphify's query surface returns structure, not the decision text, so it delivered 0 of 21 rules. Coding success fell to the no-memory baseline.
2. **It costs more than it returns.** Its contexts were about 5× larger than the notes' and 2.5× larger than TRACE's, and calls took about 3× longer. They also introduced one extra preservation failure.
3. **TRACE delivered the rule every time** and recovered 8 of the 9 notes wins. The remaining gap to notes is framing, not retrieval (see `../framing-results/REPORT.md`).
4. **Ceiling unchanged:** 13 of 24 tasks fail in every arm. The 7B editor remains the bottleneck.

## Caveats (each favors Graphify or is neutral)

- The history arm omits TRACE's 36 routine observations per task. Graphify had a cleaner corpus than TRACE.
- Extraction used qwen 7B on CPU. A stronger extraction model might produce per-decision nodes. Even then, `query` returns labels; the text would need `get_node`/`section`, which an agent would have to choose to call. Tested here: the one-shot query a session-start hook would use.
- Budget 750 tokens was chosen to fit `num_ctx` 2048. Graphify suggested 5,621 for the full neighborhood, which does not fit this model.
- Code graphs are a fair test of Graphify's main claim (codebase structure). The benchmark measures rule recall, which is not what Graphify is built for. A navigation or cross-file task suite could rank it differently.
- An npm package `@sentropic/graphify` was already on the machine and was left untouched; this trial used the PyPI `graphifyy` from Graphify-Labs.

## Files

`contexts.json` (every prompt context), `retrieval.json` / `retrieval.txt`, `full-greedy.jsonl` (120 rows + header), `regrade-full-greedy.jsonl`, `summary-full-greedy.txt`, `run.log`. Workspace and graphs: `C:\Users\pruth\graphify-eval`.
