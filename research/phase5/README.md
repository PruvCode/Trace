# TRACE Phase 5 no-handoff pilot

Frozen before the first scored model request. Compare four arms on the existing retry-backoff and partner-wire-contract fixtures. Each task uses a newly generated historical reply to a normal review request plus four fixed unrelated transcript sessions. The history prompt offers an optional project-file edit and never asks for a handoff, documentation, or memory. A proposed relative Markdown file can supply up to 1,600 characters to the notes arm.

- **No memory.** Current source and follow-up request.
- **Unprompted notes.** The same inputs plus any eligible Markdown note produced during history. If the model produces none, this arm receives exactly the no-memory input.
- **Audited TRACE.** Run the startup context and transcript APIs from audit commit `9e54673819caec3189d2c29a1ae26d5aea8a66a4` against a fresh database containing the historical transcript. The model may request one bounded transcript search.
- **Enhanced TRACE.** Run the Phase 1–4 implementation against the same raw history. Add a task binding and replay the prior executable check's status in a synthetic OpenCode-style terminal record through the actual selected pytest outcome capture API. The replay adds the test status, with no answer or manual finding. The model may request one bounded transcript search or expand one displayed evidence reference.

The product calls use separate versioned Python workers and fresh per-arm databases. Model input consists of source text, the user request, and the current arm's bounded historical data, with a 2,000-character cap per memory response. The model gets no filesystem, shell, command execution, grader or other workspace capability. It proposes a replacement for a single file in structured JSON. Grading executes proposed Python in Wasmtime with a read-only CPython standard library, bounded fuel and memory, no host project mounts and no inherited environment. Run the host canary, fuel limit, grader reference, original-source failure, empty control, notes canary, and memory search/expansion preflight before scored inference.

Use the installed local Ollama `gemma3:4b`, SHA-256 digest `a2af6cc3eb7fa8be8504abaf9b04e88f17a119ec3f04a3addf55f92841195f5a`, via loopback only. Run one generation per history, one initial generation per arm and at most one follow-up per TRACE arm, capped at 14 requests total. The spend cap is $0. Use temperature 0, seed 5021, 4,096 context tokens and 512 generated tokens per request. Shuffle arm order per task with seed 5021. Report missing or invalid model responses. Do not silently retry scored failures. [Ollama structured output API](https://ollama.com/blog/structured-outputs)

Primary outcome is pass or fail on all executable checks, paired by task. Secondary outcomes are measured input and output token counts, model wall time, lookup action, and presence of expected markers in startup and lookup results. Report each run plus per-arm pass counts. Do not use the two-task pilot to assert a product-wide advantage or native OpenCode compatibility. The fixture cases were previously used in TRACE research, and four filler sessions and tool envelopes are replayed. One historical model reply per case is live and shared across the four arms.

## Frozen source hashes

Hashes refer to LF bytes.

| File | SHA-256 |
| --- | --- |
| `cases.py` | `b2ab8b1be79f4156d3ec6515eab2ae596e18b29ced174a54bc227018c04e040c` |
| `grader.py` | `cd6f574d0274a4838dee04bdd656fdb28a680891673f77a8e48150f8c51e4f84` |
| `memory_worker.py` | `5a64a5a206c854e8e72c6770e49d91d1713d164c51a4010bcd179ed6191edc57` |
| `pilot.py` | `9f78845e09e17452c289ab764849884d76bbce2ee244273109e4095673bc5dfd` |

## Reproduce

Use a separate environment and install `wasmtime==45.0.0` as a research dependency. Extract the public `python-3.13.15-wasi_sdk-24.zip` into WASI_ROOT. Archive SHA-256 is `67e1c32a85d5e0600c0939f5fd4023ca0127f1a9e81f5499697467bda67af78d`. AUDIT_ROOT points to a tracked export of the audit commit, and SCRATCH points to an existing disposable directory. [Pinned CPython WASI release](https://github.com/brettcannon/cpython-wasi-build/releases/tag/v3.13.15) · [Wasmtime security model](https://docs.wasmtime.dev/security.html)

```text
python -m research.phase5.pilot --audit-root AUDIT_ROOT --wasi-root WASI_ROOT --scratch SCRATCH
python -m research.phase5.pilot --audit-root AUDIT_ROOT --wasi-root WASI_ROOT --scratch SCRATCH --live-local
```

The first command runs the model-free preflight. The second reruns preflight and requires the already-installed pinned model. Results are JSON Lines. The model's proposed source is recorded as data and evaluated inside WASI.
