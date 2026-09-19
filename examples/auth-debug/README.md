# Example: continuing an auth investigation across sessions

A tiny walkthrough of the core TRACE loop. No benchmark, no API keys, no
network. Everything below runs locally with the `trace` CLI.

Setup — use this example directory as the project:

```powershell
cd examples/auth-debug
trace init
```

Expected output (numbers may vary slightly):

```text
TRACE initialized: ...\examples\auth-debug
  database:   ...\examples\auth-debug\.agent-memory\memory.db
  files:      1
  symbols:    2
  relations:  2
  next: connect an agent with 'trace mcp', inspect with 'trace status'
```

## Session 1 — investigate, record findings

You (or your agent) look at the timeout handling and record what you learn:

```powershell
trace find get_session_timeout .
trace callers get_session_timeout .

trace record --type investigation --symbol get_session_timeout `
  --message "timeout flows auth.get_session_timeout -> refresh_token"

trace record --type decision --symbol get_session_timeout `
  --message "keep default 30 minutes; revisit after login rollout"
```

## Session 2 — a new session retrieves the context

Later (a new agent session, a new terminal, a new day), the context is
still there:

```powershell
trace events --symbol get_session_timeout .
```

```text
{"commit": null, "file": null, "id": 1, "payload": {"message": "timeout flows ..."}, ...}
{"commit": null, "file": null, "id": 2, "payload": {"message": "keep default ..."}, ...}
```

An MCP-connected agent does the same thing through `search_events` /
`find_definition` instead of the CLI, so findings survive across sessions
without pasting transcripts around.

## Inspect and clean up

```powershell
trace status .
trace report .
trace reset . --yes   # deletes .agent-memory for this example only
```

Memory is per-project: initializing this example never touches memory from
any other directory.
