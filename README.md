# TRACE — Benchmark/Evaluation Harness for Coding-Agent Memory

> The benchmark is the product. Memory is the reference system being evaluated.
> Core question: does persistent local structural + episodic memory help coding
> agents solve software-engineering tasks while reducing redundant
> context/token consumption vs. a baseline without memory?

## Status: Phase 0 (V0 — Benchmark Foundation, scaffolding only)

Phase 0 proves: the project builds and tests reproducibly. It proves nothing
about tasks, agents, or memory. See the approved plan (V0/V1/V2) for the full
roadmap:

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

## Reproducibility notes

- Pinned starting point: `pyproject.toml` lower bounds (`requires-python >=3.11`,
  `pyyaml`, `pytest`); full lockfiles arrive with Phase 1.
- Run outputs go to `runs/` (gitignored). Memory state goes to
  `.agent-memory/` (gitignored).
- OneDrive-managed checkouts can stall SQLite/git worktrees. For real
  experiments, clone the repo outside OneDrive or pass a `--work-root`
  outside OneDrive (available from Phase 1).

## What is NOT here yet

Runner, agents, evaluator, memory backend, MCP server, tasks — all arrive in
their planned phases. Phase 0 is intentionally only: `pyproject.toml`,
`.gitignore`, `README.md`, placeholder test.
