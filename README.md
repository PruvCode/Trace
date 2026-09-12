# TRACE — Benchmark/Evaluation Harness for Coding-Agent Memory

> The benchmark is the product. Memory is the reference system being evaluated.
> Core question: does persistent local structural + episodic memory help coding
> agents solve software-engineering tasks while reducing redundant
> context/token consumption vs. a baseline without memory?

## Status: Phase 2 (V1 — Reference Memory, structural only)

Phase 2 proves: structural memory can expose useful code relationships
(Tree-sitter → SQLite/FTS5 → MCP tools `find_definition`, `find_callers`,
`search_symbols`). It does NOT prove that structural memory improves agent
performance (no agent wiring yet — Phase 4).

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

## What is NOT here yet (Phases 3+: episodic, wiring)

Episodic memory, Git-derived events, transcripts, the real LLM agent, and
B/C/D task categories. Reference-memory MCP tools are queryable directly
(`python -m memory.mcp_server --db <workspace>/.agent-memory/memory.db`)
but no agent is wired to them yet.
