"""Project-local OpenCode plugin that injects TRACE session-start memory.

TRACE owns retrieval (memory.message_context over memory.session_context);
this module only owns the thin JavaScript glue installed into
``.opencode/plugins/trace-memory.js``:

- registers ``experimental.chat.messages.transform`` (verified on 1.18.32)
- runs ``python -m memory.message_context`` for the project directory
- unshifts one clearly labeled synthetic user-channel message carrying a
  bounded prior-memory block, once per session
- the messages.transform hook carries no session identifier, so the
  helper resolves the current session from OpenCode's own session table
  (newest session row for this project directory) and the plugin guards
  on that resolved identifier; unresolvable sessions inject nothing
- appends a content-free JSON diagnostic line per attempt to
  ``.agent-memory/retrieval.log``
  (sessionID, channel, items, chars, injected, latency, error, reason)
- fails open on any error: OpenCode must keep working without memory

No SQLite in JS. No npm dependencies. No network.
"""

from __future__ import annotations

import json
from pathlib import Path

from trace_memory import project as project_mod

PLUGIN_FILENAME = "trace-memory.js"
PLUGIN_DIRNAME = ".opencode/plugins"


def _js_string(value: str) -> str:
    """Render a Python string as a JS string literal (backslash-safe)."""
    return json.dumps(value)


def default_opencode_db_path() -> Path:
    """Locate OpenCode's global database (same resolution as the monitor)."""
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


PLUGIN_JS_TEMPLATE = """\
// TRACE automatic session-start memory (installed by TRACE setup).
// PATHS BAKED AT INSTALL TIME - regenerated on every setup_integration.
const TRACE_PYTHON = __TRACE_PYTHON__;
const TRACE_DB = __TRACE_DB__;
const OPENCODE_DB = __OPENCODE_DB__;

const traceMsgInjectedSessions = new Set();

export const TraceMemoryPlugin = async ({ directory }) => {
  return {
    "experimental.chat.messages.transform": async (input, output) => {
      const started = Date.now();
      let sessionID = "unknown";
      let items = 0;
      let chars = 0;
      let injected = false;
      let error = null;
      let reason = null;
      try {
        // The messages.transform hook carries no session identifier, so the
        // helper resolves the current session from OpenCode's session table
        // (newest row for this project directory) and excludes it from
        // retrieval. Anything ambiguous resolves to no injection (fail open).
        // Guard: inject once per resolved session. Later requests already
        // carry accumulated turns; re-injection would only duplicate tokens.
        // Marked only on successful push, so a failed first attempt may
        // still succeed once later in the same session.
        if (!output || !Array.isArray(output.messages)) {
          return;
        }
        const proc = Bun.spawnSync([
          TRACE_PYTHON,
          "-m",
          "memory.message_context",
          "--db",
          TRACE_DB,
          "--opencode-db",
          OPENCODE_DB,
          "--project-dir",
          directory,
        ], { cwd: directory, stderr: "pipe", stdout: "pipe" });
        const block = proc.stdout ? proc.stdout.toString().trim() : "";
        let stats = {};
        try {
          stats = JSON.parse(proc.stderr ? proc.stderr.toString() : "{}");
        } catch {}
        sessionID = stats.currentSession || "unknown";
        items = stats.items || 0;
        chars = stats.chars || 0;
        reason = stats.reason || null;
        if (proc.exitCode === 0 && block && stats.resolved === true && sessionID !== "unknown") {
          if (traceMsgInjectedSessions.has(sessionID)) {
            return;
          }
          output.messages.unshift({
            info: { role: "user" },
            parts: [{ type: "text", synthetic: true, text: block }],
          });
          traceMsgInjectedSessions.add(sessionID);
          injected = true;
        }
      } catch (e) {
        error = String((e && e.message) || e);
      } finally {
        try {
          const fs2 = await import("node:fs/promises");
          const path2 = await import("node:path");
          const line = JSON.stringify({
            ts: new Date().toISOString(),
            sessionID,
            channel: "messages.transform",
            items,
            chars,
            injected,
            latencyMs: Date.now() - started,
            error,
            reason,
          });
          await fs2.appendFile(path2.join(path2.dirname(TRACE_DB), "retrieval.log"), line + "\\n");
        } catch {}
      }
    },
  };
};
"""


def render_plugin(python_exe: str, trace_db: str, opencode_db: str) -> str:
    """Render the plugin source with install-time paths baked in."""
    return (
        PLUGIN_JS_TEMPLATE
        .replace("__TRACE_PYTHON__", _js_string(python_exe))
        .replace("__TRACE_DB__", _js_string(trace_db))
        .replace("__OPENCODE_DB__", _js_string(opencode_db))
    )


def install_plugin(project_path: Path, opencode_db_path: Path | None = None) -> dict:
    """Write the project-local TRACE plugin. Returns status info."""
    project_path = project_path.resolve()
    trace_db = str(project_mod.db_path(project_path))
    python_exe = project_mod.mcp_server_command(project_path)[0]
    opencode_db = str(opencode_db_path) if opencode_db_path is not None else str(default_opencode_db_path())
    plugin_path = project_path / PLUGIN_DIRNAME / PLUGIN_FILENAME
    plugin_path.parent.mkdir(parents=True, exist_ok=True)
    existed = plugin_path.exists()
    plugin_path.write_text(render_plugin(python_exe, trace_db, opencode_db), encoding="utf-8")
    return {
        "status": "updated" if existed else "created",
        "plugin_path": str(plugin_path),
        "message": "OpenCode TRACE memory plugin installed",
    }
