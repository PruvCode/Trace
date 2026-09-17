# TRACE

**TRACE gives coding agents persistent local memory across sessions — and measures whether that memory actually helps.**

```powershell
git clone <repo-url> TRACE
cd TRACE
python -m pip install -e .
trace init
trace mcp        # connect an MCP-compatible agent
trace status     # inspect what TRACE stored
```

## Why TRACE?

Coding agents forget everything between sessions. Every new session
re-discovers the same definitions, re-investigates the same bugs, and
re-makes the same decisions.

TRACE records four kinds of memory locally — what the code **is**, what
**happened**, what **changed**, and what **was discussed** — and exposes them to agents through MCP.
Its strongest differentiator: **TRACE doesn't ask you to believe that
memory works. It gives you the infrastructure to measure it.**

## How it works

| Memory | Question it answers | Source |
|---|---|---|
| Structural | What IS this codebase? (definitions, callers, symbols) | tree-sitter index of your code |
| Episodic | What HAPPENED? (investigations, attempts, decisions) | recorded by agents / you, per session |
| Git | What CHANGED? (commits, files, touched symbols) | deterministic facts from local git history |
| Transcript | What was DISCUSSED? (conversations, sessions) | recorded by agents / you, per session |

```
your project                    agent (any MCP-compatible client)
     │                                    │
     ▼                                    ▼
.agent-memory/memory.db  ◄── stdio ── MCP server (9 tools)
(SQLite + FTS5, local)        find_definition / find_callers /
                              search_symbols / record_event /
                              search_events / get_git_history /
                              record_transcript_message /
                              search_transcripts / get_transcript_session
```

Session N records findings → Session N+1 retrieves them. See
[`examples/auth-debug`](examples/auth-debug) for a 5-minute walkthrough.

## Installation

Requirements: Python 3.11+. TRACE is installed from source:

```powershell
git clone <repo-url> TRACE
cd TRACE
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
```

For development (includes pytest):

```powershell
python -m pip install -e ".[dev]"
```

## Quick Start

Inside any project directory:

```powershell
trace init
```

```text
TRACE initialized: C:\work\myproj
  database:   C:\work\myproj\.agent-memory\memory.db
  files:      42
  symbols:    310
  relations:  518
  next: connect an agent with 'trace mcp', inspect with 'trace status'
```

Then, across sessions:

```powershell
# what TRACE knows
trace status
trace find get_session_timeout
trace callers refresh_token
trace search "timeout"

# record a finding (agents do this via record_event)
trace record --type investigation --symbol get_session_timeout `
  --message "timeout flows get_session_timeout -> refresh_token"

# retrieve it later (agents do this via search_events)
trace events --symbol get_session_timeout

# record/search transcript memory
trace transcript record --session-id abc123 --role user --content "We found the bug"
trace transcript record --session-id abc123 --role assistant --content "The fix is to reorder middleware"
trace transcript search --query "middleware"
trace transcript show --session-id abc123

# full terminal summary
trace report

# re-index after big changes, delete when done
trace index
trace reset . --yes
```

`trace init` is idempotent — re-running it re-indexes structural memory
while keeping recorded events.

## Agent Integration

TRACE exposes memory over [MCP](https://modelcontextprotocol.io/) (stdio,
local process). Any MCP-compatible agent can use it:

1. Run `trace init` in your project.
2. Run `trace mcp` — it prints a config snippet pointing at your project's
   local database:

```json
{
  "mcpServers": {
    "trace-memory": {
      "command": ["python", "-m", "memory.mcp_server",
                  "--db", "<project>/.agent-memory/memory.db",
                  "--repo", "<project>"]
    }
  }
}
```

3. Add that snippet to your agent's MCP configuration.
4. The agent can now call the 9 memory tools; everything it records
   persists in `<project>/.agent-memory/memory.db` for the next session.

Supported: any agent that speaks MCP over stdio and can spawn a local
command (e.g. Claude Code, OpenCode with MCP configured, other
MCP-compatible clients). Only MCP-compatible agents are supported — TRACE
does not claim universal compatibility, and untested clients should be
treated as experimental.

## Memory Storage

- One database per project: `<project>/.agent-memory/memory.db`
  (SQLite + FTS5). Projects never share memory.
- Add `.agent-memory/` to your project's `.gitignore` (`trace init`
  reminds you inside git repositories) so local memory stays local.
- Structural rows are rebuilt deterministically on `trace index`;
  episodic events are append-only until you run `trace reset`.
- All queries are parameterized and result-bounded (default 10, max 50).
- Inspect with `trace status` / `trace report`, or open the SQLite file
  with any SQLite browser.

## Measurement

TRACE measures token usage so you can evaluate whether memory helps your
workflow. Per-run accounting (input/output/total tokens, turns, tool
calls, memory tool calls, latency) is recorded by the benchmark runner,
and summarized descriptively:

```powershell
python -m benchmark.runner --task tasks/A_control/task_01_timeout_fix --config configs/baseline.yaml
python -m benchmark.analyze --results runs/workspaces/<exp_id>/results.jsonl --tasks-root tasks
```

Token counts come from exact provider `usage`, never estimates. TRACE
makes no claim that memory saves tokens — it gives you the numbers to
compare baseline vs. memory runs yourself.

## Privacy

**Your code, memory, and transcripts remain on your machine.**

- No telemetry, no analytics, no accounts, no cloud sync.
- TRACE itself makes no network calls: storage is SQLite, indexing is
  local tree-sitter, the MCP server is a local stdio process.
- Transcript memory is stored in the same local SQLite database as other
  memory layers, inside `<project>/.agent-memory/memory.db`.
- The only network traffic in the system is between *your agent* and *your
  chosen model provider*, when you run an LLM-backed agent — TRACE's
  memory storage is never sent anywhere.

## Benchmark / Research

`benchmark/` is experimental research infrastructure for the question
"does memory help?": a task corpus (`tasks/`), baseline-vs-memory configs
(`configs/`), an isolated runner, and descriptive analysis. Results are
labeled pilot/calibration, never evidence of effectiveness. The
SWE-bench/Django corpus work in the tree is unfinished and does not affect
daily TRACE use.

## Supported Platforms

Developed on Windows (PowerShell 5.1); intended to work on Linux/macOS
with Python 3.11+. Git-history features need a git repository. Temporary
and run directories should live outside cloud-synced folders (OneDrive,
etc.) for performance.

## Development

```powershell
python -m pip install -e ".[dev]"
# Run tests with a temp dir OUTSIDE OneDrive/cloud-synced folders:
python -m pytest -q --no-header -p no:cacheprovider --basetemp="$env:TEMP\TracePytest"
```

Architecture rules: `memory/` owns storage, `benchmark/runner.py` is the
sole benchmark orchestrator, `agent/` stays provider-neutral, and the new
`trace_memory/` CLI package only wraps `memory.*` (it never imports the benchmark).

## Testing

Full suite: 282+ tests, all local —
no network, keys, or models required. Real-model runs are manual-only.

## Limitations

- Structural indexing covers Python only; other languages are invisible
  to structural memory (episodic/git/transcript memory still work).
- Same-named methods on different classes can conflate in call edges;
  symbol attribution from diffs is best-effort overlap, not proof.
- Transcript memory is session-scoped and requires explicit recording
  (via CLI, MCP, or supported agent integration). It does not automatically
  capture all agent conversations.
- Benchmark evidence is pilot-scale; no effectiveness claims are made.
- MCP integration is labeled by what it is (MCP-compatible agents),
  not by an exhaustive tested-client matrix.

## Roadmap

- More languages for structural indexing
- Broader tested-agent matrix
- Non-goals: cloud sync, vector DBs, embeddings service, web dashboard,
  telemetry — TRACE stays local-first and simple.

## Contributing

Issues and PRs welcome. Keep changes small, local-first, and tested;
don't add services, network calls, or dependencies without discussion.

## License

MIT — see [LICENSE](LICENSE).
