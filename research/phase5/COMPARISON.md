# Phase 5 controlled comparison

Frozen before qualification and scored runs. This comparison follows the two-case no-handoff pilot in this directory. The pilot remains a separate result.

## Design

- Use six new synthetic single-function maintenance tasks with new rules determined by prior investigations. Hold three other tasks for model qualification. The candidate sees one source file as text. The independent Wasmtime grader checks the requested behavior, boundaries and output types, with no host workspace or environment mounted. The grading tests and reference values outside the assigned memory are withheld from the model.
- Compare no memory, best-case prompted notes, audited TRACE at `9e54673`, and enhanced TRACE with same-task startup transcript retrieval. Each TRACE arm seeds a separate SQLite database from the same scripted history and exposes one bounded read-only lookup. Enhanced TRACE additionally receives an explicit session binding and the synthetic prior test status through the existing outcome capture API. The best-case notes arm receives one human-authored Markdown note containing the decision. It represents the result of a successful handoff. There is no note in the no-handoff replay, so unprompted notes and no memory share the same scored row. This controls note availability. It does not measure the behavioral effect of prompting someone to write a note.
- Use the same requests, task order, source, and grading rules. Shuffled arm order uses seed 5041. The plain Python response may be fenced. Reject syntax errors and generation truncation. TRACE arms may instead first request one transcript search or, with a displayed reference, an evidence page. Context and lookup responses each cap at 2,000 rendered characters. The synthetic decision, unrelated transcript filler, and simulated test status are fixed. Native OpenCode, real project histories, and a live prompted handoff are outside this comparison.
- Qualify the installed Gemma 3 4B model on three disjoint tasks with the needed facts supplied. Require at least two executable passes before scoring. If it fails, qualify Qwen2.5-Coder 3B locally using the same rules before considering scored calls. If neither passes, publish the failed gates and the deterministic retrieval audit. Use the same qualified model and pinned digest throughout scored runs. CPU/GPU selection stays with the installed Ollama service. Use loopback, seed 5041, temperature 0, 2,048 context tokens, 128 generated tokens and no paid calls. Maximum three calls per qualification model and 36 scored requests. [Local Ollama chat API](https://docs.ollama.com/api/chat)
- Report six matched comparisons, executable pass counts, syntax-valid counts, startup decision coverage, retention of one distinctive prior behavior, successful and attempted lookups, available evidence references, model tokens and latency. Scores treat truncated or invalid responses as failures. A separate regrade of saved proposals must reproduce the results. A small synthetic sample and one qualified model cannot establish a product-wide effect. Record ties and task-level differences alongside counts.

The source hashes below refer to LF bytes.

| File | SHA-256 |
| --- | --- |
| `comparison_cases.py` | `8bbeec2696b098b92fd25694bb55d23d6135d56c91592fe6874edefba06ea480` |
| `comparison.py` | `2940a5790de9ec029b42a31d931e97a62d53083284207d3144e71afea63c2d30` |

## Reproduce

Use the audit export and pinned WASI setup documented in [the pilot protocol](README.md). Set AUDIT_ROOT to the tracked export of the audit commit, WASI_ROOT to the verified WASI runtime, and SCRATCH to an existing disposable directory.

```text
python -m research.phase5.comparison qualify --audit-root AUDIT_ROOT --wasi-root WASI_ROOT --scratch SCRATCH
python -m research.phase5.comparison score --audit-root AUDIT_ROOT --wasi-root WASI_ROOT --scratch SCRATCH --input QUALIFICATION_JSONL
python -m research.phase5.comparison regrade --audit-root AUDIT_ROOT --wasi-root WASI_ROOT --scratch SCRATCH --input SCORED_JSONL
```

Each command writes JSON Lines to stdout. The score command requires the qualification output for the same model and digest. `--model` and `--digest` select a different installed local model for both steps. The scored results contain generated source as data.
