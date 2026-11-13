# TRACE Phase 4 retrieval protocol

Frozen before the first scored run on November 11, 2026. Changes to the method or fixtures after scoring require a separate run and disclosure.

- Fixture SHA-256: `603aa16432a65fd48efe41a1112a1e70108458215499586af7ab6cdd3de51f20`
- Harness SHA-256: `9594e0485b71ec4a847c8e9e45009b78151782f5540c44d57eaa4b995ee8078b`
- Baseline: TRACE's Phase 3 task-bound startup function, including SQLite FTS and its recency fallback, on audit commit `9e54673819caec3189d2c29a1ae26d5aea8a66a4` with the Phase 1–3 patch.
- Candidate: the same baseline plus local MiniLM cosine retrieval over up to the 50 newest ordinary observations in that task. Fuse the five baseline candidates and up to 20 semantic candidates using weighted reciprocal ranks, with weights 0.35 and 0.65 and constant 10. Break ties by event ID. A syntactic identifier rule routes CamelCase and dotted, underscored, or hyphenated names to the baseline. The rule uses query text alone.
- Input: 36 held-out queries across six synthetic tasks, with four paraphrases and two exact identifiers per task, and one author-assigned relevant note per query. Two other synthetic tasks with four queries total validate and warm the harness. Each held-out task has 44 notes, including 32 newer generic observations and six query-shaped decoys for another task. All observations have the same lifecycle and evidence status. No private history, external model calls, live agents, or corpus derived from TRACE users.
- Both arms receive the same query and explicit task ID, and may emit at most five items and 2,000 characters including TRACE's framing. Score the rank of the labeled event in that bounded output, and count any item from outside the requested task. Report recall and MRR at five, per-case ranks, wrong-task items, and rendered sizes. Use a 2,000-draw task-cluster bootstrap interval as a descriptive sensitivity check.
- On Windows, use CPU-only ONNX Runtime with one inter-op and one intra-op thread. Use 128-token truncation, masked mean pooling, and L2 normalization. Verify the pinned files before each model session. Corpus vectors are precomputed in memory. No query-vector cache. Warm timing runs five shuffled paired repetitions with seed 4061, using the nearest-rank p95. Report query lookup p50 and p95, initial load, held-out index build, resident raw vector size, DB and model sizes, and six fresh ONNX sessions using a warm OS file cache. The latter measures model load plus first inference with an existing corpus index, so it is distinct from process or disk cold start.
- Keep optional embeddings only if the candidate gains at least 10 absolute percentage points of paraphrase Recall@5, loses zero baseline exact hits, emits zero wrong-task items in both arms, and warm paraphrase lookup p95 is no more than 1.25 times baseline. Report exact and combined latency as well. The paraphrase stratum measures the path where semantic inference runs. Simulate unavailable embeddings and failed inference to verify exact fallback output.

The two arms evaluate retrieval within ordinary task observations. The fixture has one author and one relevant item per query. The holdout is separate from the smoke cases but has no independent annotations. The replay cannot establish open-ended retrieval quality, scaling across large histories, or coding-agent task success.

## Reproduce

Use a separate Python 3.11 environment with TRACE's normal dependencies and these research-only dependencies.

```text
python -m pip install numpy==2.2.6 onnxruntime==1.22.1 tokenizers==0.21.4
python -m research.phase4.retrieval_eval --model-dir MODEL_DIR --scratch EXISTING_SCRATCH_DIR
```

From the [pinned public model revision](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/tree/1110a243fdf4706b3f48f1d95db1a4f5529b4d41), place `onnx/model_quint8_avx2.onnx` and `tokenizer.json` directly in MODEL_DIR. The harness verifies both hashes and runs locally. Its two SQLite databases are created in a new temporary directory inside the supplied scratch directory. The JSON on stdout contains the scores, raw timings, and gate decision.
