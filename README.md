# TRACE — Benchmark/Evaluation Harness for Coding-Agent Memory

> The benchmark is the product. Memory is the reference system being evaluated.
> Core question: does persistent local structural + episodic memory help coding
> agents solve software-engineering tasks while reducing redundant
> context/token consumption vs. a baseline without memory?

## Status: Phase 4 (controlled agent ↔ memory integration)

Phase 4 proves: TRACE can connect a coding agent to its reference memory
system under a controlled benchmark configuration. It does NOT prove that
memory improves coding-agent performance (that requires Phase 5+ experiments).

- V0 (Phase 0+1): reproducibly execute + evaluate coding tasks without memory.
- V1 (Phase 2+3+4): structural + episodic reference memory behind MCP.
- V2 (Phase 5+6): run A/B/C/D × baseline/memory/external and compare
  success, tokens, latency, turns, tool calls.

## Setup (Windows PowerShell 5.1)

The only interpreter guaranteed on this machine is the hermes-agent one
(without `pip`). Bootstrap an isolated project venv with either:

```powershell
# Option A: stdlib venv + ensurepip (no extra tools)
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m ensurepip --upgrade
python -m pip install -e ".[dev]"

# Option B: uv (available at %LocalAppData%\hermes\bin\uv.exe)
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
```

Then:

```powershell
python -m pytest
```

## Running the benchmark (Phase 1: baseline + mock agent only)

```powershell
# Seed the fixture repo (deterministic SHA, printed for task.yaml)
python scripts/seed_toy_repo.py

# One task, baseline config, two runs
python -m benchmark.runner --task tasks/A_control/task_01_timeout_fix --config configs/baseline.yaml --runs 2

# Failing-outcome demo (evaluator still decides, not the agent)
python -m benchmark.runner --task tasks/A_control/task_01_timeout_fix --config configs/baseline.yaml --mock-behavior fail
```

Results append to `runs/workspaces/<exp_id>/results.jsonl`, one JSON line per
run with full provenance (seed, task/config hashes, base commit, exit code,
tool log, git status).

## Reproducibility notes

- Pinned starting point: `pyproject.toml` lower bounds (`requires-python >=3.11`,
  `pyyaml`, `pytest`); full lockfiles arrive with Phase 1.
- Run outputs go to `runs/` (gitignored). Memory state goes to
  `.agent-memory/` (gitignored).
- OneDrive-managed checkouts can stall SQLite/git worktrees. For real
  experiments, clone the repo outside OneDrive or pass a `--work-root`
  outside OneDrive (available from Phase 1).

## What is NOT here yet (Phase 5+: experiments)

B/C/D task categories, staleness experiments, external backends, and any
claim about memory effectiveness.

## Benchmark corpus (Phase 5.1: Category A — Control)

`tasks/A_control/` holds simple single-file tasks where memory should
provide little or no advantage (`task_01_timeout_fix`,
`task_02_empty_user`). Each task is mechanically evaluated and must fail on
the clean fixture and pass after the correct fix. `scripts/pilot.py` runs a
task × configuration matrix through the existing runner; its output is
labelled PILOT and is calibration only, never evidence.

`tasks/B_structural/` holds cross-file tasks on `fixtures/deps_repo`
(`task_01_callers`, `task_02_definition`). Each requires understanding a
definition→consumer relationship; evaluators assert behavior, never tool
strategy. Baseline and reference run every task under identical controls.

## Configurations (Phase 4)

```powershell
# Baseline: core tools only (read_file, write_file, done)
python -m benchmark.runner --task tasks/A_control/task_01_timeout_fix --config configs/baseline.yaml

# Reference memory: same core tools + 6 memory tools over MCP
python -m benchmark.runner --task tasks/A_control/task_01_timeout_fix --config configs/reference_memory.yaml
```

Both runs share task, base commit, agent, model, budget, timeout, and
evaluator. The only difference is memory availability (plus a fixed
tool-availability sentence in the prompt, versioned in the config).
`tests/benchmark/test_config_fairness.py` enforces this mechanically.

## How memory is isolated

Each run clones a fresh workspace; `ReferenceBackend.setup()` builds a fresh
`<workspace>/.agent-memory/memory.db` inside it (structural index + optional
explicit `preseed` events) and spawns one MCP server child for that run
only. Teardown kills the child. Two runs never share a database or process.

## How MCP is started

`ReferenceBackend` prepares the database, then `benchmark/mcp_bridge.py`
spawns `python -m memory.mcp_server --db <db> --repo <workspace>` over stdio,
initializes a client session, and exposes the 6 tools as normal `ToolDef`s.
Transport failure fails closed per tool call; setup failure marks the run
`memory_setup_failed` (never silent baseline).

## How token accounting works

`LLMAgent` sums exact provider `usage` per turn into
`AgentResult.input/output_tokens`; the runner writes `total_tokens` only
when both are known, plus `token_source: provider|unknown`. Mocks report
`None`/`unknown`. Nothing is ever estimated. Memory vs total calls are split
via `memory_tool_calls` (counted from the existing tool log).

## What the benchmark does and does not prove

Proves: agent↔memory runs execute reproducibly with honest failure records.
Does not prove: any performance effect of memory — Phase 4 has one control
task and mock agents by design.

## Known limitations

Real-model runs need `TRACE_API_KEY`/`OPENAI_API_KEY` and are manual-only
(never in CI); per-request timeouts default to 60s; `MockAgent` memory
behavior is scripted plumbing, not intelligence; Windows stdio spawn adds
~1–2s per reference run.

## Deterministic tests

```powershell
python -m pytest
```

No test needs network, keys, or a model. The `openai` package is imported
only by the real-client adapter, which no automated test constructs
against the network (missing-key behavior is tested with env scrubbed).
