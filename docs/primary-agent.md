# Primary Agent Selection

## Decision

**Primary Agent: OpenCode**

## Why Selected

OpenCode is the only agent with a **working, tested, end-to-end transparent capture implementation** in TRACE today.

- Transparent capture via database monitoring (`memory/opencode_transparent.py`) — 24 tests passing
- MCP server integration configured via `opencode.json` — `trace setup opencode --transparent` works
- Automatic session/message/part capture from OpenCode's SQLite database
- Secret redaction before persistence
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
- Agent-mediated retrieval (not automatic injection)

## How TRACE Runs Automatically

1. **One-time setup**: `trace setup opencode --transparent`
   - Creates `opencode.json` with MCP server config
   - Starts background database monitor

2. **Normal usage**: User runs `opencode` normally
   - Monitor detects new sessions/messages/parts automatically
   - No wrapper commands needed

3. **Next session**: User runs `opencode` again
   - MCP server provides `search_transcripts`, `get_transcript_session`, `search_events`
   - Agent retrieves relevant context on demand

## How Startup Context Can Be Injected

- **MCP-mediated**: Agent calls `search_transcripts` / `get_transcript_session` / `search_events` when needed
- **No automatic injection**: TRACE does not inject context automatically; the agent decides when to retrieve
- **SessionStart hook** (future): Could inject context via `additionalContext` output, but not currently implemented

## Important Limitations

1. **OpenCode only** — No support for Claude Code or Codex
2. **Local OpenCode database required** — User must have run OpenCode at least once
3. **No automatic context injection** — Agent must explicitly call MCP tools
4. **Secrets redaction is best-effort** — Pattern-based, not guaranteed
5. **Windows/Linux/macOS** — Database path differs (`~/.local/share/opencode/opencode.db` on Linux/macOS, `%APPDATA%\opencode\opencode.db` on Windows — currently only Unix path implemented)

## Other Agents

| Agent | Status |
|-------|--------|
| Claude Code | **Unsupported** — No integration code exists |
| Codex | **Unsupported** — No integration code exists; hooks experimental |

Both remain explicitly **unsupported**. Future work may add them, but Step 3+ will only target OpenCode.