# Primary Agent Selection

## Decision

**Primary Agent: OpenCode**

## Why Selected

OpenCode is the only agent with a **working, tested, end-to-end automatic memory implementation** in TRACE today (validated with OpenCode 1.18.32).

- Transparent capture via database monitoring (`memory/opencode_transparent.py`)
- Automatic session-start injection via a project-local plugin (`memory/opencode_plugin.py`, `memory/message_context.py`) — one labeled user-channel message per session, resolved against OpenCode's own session table, fail-open on ambiguity
- MCP server integration configured via `opencode.json` — `trace setup opencode --transparent` works
- Automatic session/message/part capture from OpenCode's SQLite database
- Secret redaction before persistence and before injection (best-effort)
- Restart recovery with catch-up logic
- Idempotent processing with ROWID-based cursors

No other agent has any integration code in TRACE.

## Actual Hook/Session Mechanism

| Mechanism | Status |
|-----------|--------|
| Session lifecycle hooks (session.created, session.deleted, session.idle) | Available via hooks.yaml |
| File change hooks (file.changed) | Available |
| Tool lifecycle hooks (tool.before.*, tool.after.*) | Available |
| **Database monitoring (transparent capture)** | **IMPLEMENTED & TESTED** |
| **User-channel memory injection (messages.transform)** | **IMPLEMENTED & TESTED** |
| MCP server integration | **IMPLEMENTED & TESTED** |

## How TRACE Receives Session/Activity Data

**Primary: Database Monitor** (`OpenCodeDatabaseMonitor`)

- Polls `~/.local/share/opencode/opencode.db` every 1 second
- Uses ROWID-based incremental cursors for sessions, messages, parts
- Captures: user messages, assistant responses, tool calls (including TRACE memory tools)
- Persists to TRACE transcript memory + episodic events
- Interruptible stop event for clean shutdown

**Secondary: MCP Server** (`trace_memory.mcp_server`)

- Configured via `opencode.json` created by `trace setup opencode --transparent`
- Exposes 9 tools: find_definition, find_callers, search_symbols, record_event, search_events, get_git_history, record_transcript_message, search_transcripts, get_transcript_session
- Explicit on-demand querying by the agent (or via `trace` CLI commands); complements automatic injection, does not replace it

## How TRACE Runs Automatically

1. **One-time setup**: `trace setup opencode --transparent`
   - Creates `opencode.json` with MCP server config
   - Installs the project-local retrieval plugin (`.opencode/plugins/trace-memory.js`)
   - Starts background database monitor

2. **Normal usage**: User runs `opencode` normally
   - Monitor detects new sessions/messages/parts automatically
   - No wrapper commands needed

3. **Next session**: User runs `opencode` again
   - Plugin resolves the current session, retrieves bounded prior memory, and injects one labeled contextual message automatically
   - MCP tools remain for explicit lookups (`search_transcripts`, `get_transcript_session`, `search_events`)

## How Startup Context Is Injected

- **Automatic (primary)**: the project-local plugin's `experimental.chat.messages.transform` hook unshifts one `[TRACE PROJECT MEMORY - ...]` user-channel message per session (bounded: 10 tail messages + 5 findings, 20 items / 4000 chars, project-scoped, redacted, fail-open). Validated with OpenCode 1.18.32; do not assume identical hooks in other versions.
- **MCP-mediated (explicit)**: Agent calls `search_transcripts` / `get_transcript_session` / `search_events` when it wants to look something up on demand.

## Important Limitations

1. **OpenCode only** — No support for Claude Code or Codex
2. **Local OpenCode database required** — User must have run OpenCode at least once
3. **Resolution fails open** — Continued/stale sessions, TUI multi-session use, and concurrent same-project runs may receive no automatic injection rather than a wrong one
4. **Model usefulness is bounded** — Single unambiguous prior facts recall cleanly in testing (9/9 valid trials); multi-fact selection is weaker. No coding-effectiveness claims.
5. **Secrets redaction is best-effort** — Pattern-based, not guaranteed
6. **Windows/Linux/macOS** — The OpenCode database is resolved at `~/.local/share/opencode/opencode.db` on all platforms (the location verified on the Windows dev machine); run OpenCode at least once first

## Other Agents

| Agent | Status |
|-------|--------|
| Claude Code | **Unsupported** — No integration code exists |
| Codex | **Unsupported** — No integration code exists; hooks experimental |

Both remain explicitly **unsupported**. Future work may add them, but Step 3+ will only target OpenCode.