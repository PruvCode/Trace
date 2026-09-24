"""Project-local OpenCode plugin that injects TRACE session-start memory.

TRACE owns retrieval (memory.session_context); this module only owns the
thin JavaScript glue installed into ``.opencode/plugins/trace-memory.js``:

- registers ``experimental.chat.system.transform`` (verified on 1.18.32)
- runs ``python -m memory.session_context`` for the current session
- pushes a bounded prior-memory block into model-visible context once
  per session (session-local guard; later requests carry history already)
- appends a content-free JSON diagnostic line per attempt to
  ``.agent-memory/retrieval.log`` (items, chars, injected, latency, error)
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


PLUGIN_JS_TEMPLATE = """\
// TRACE automatic session-start memory (installed by TRACE setup).
// PATHS BAKED AT INSTALL TIME - regenerated on every setup_integration.
const TRACE_PYTHON = __TRACE_PYTHON__;
const TRACE_DB = __TRACE_DB__;

const traceInjectedSessions = new Set();

export const TraceMemoryPlugin = async ({ directory }) => {
  return {
    "experimental.chat.system.transform": async (input, output) => {
      const started = Date.now();
      const sessionID = (input && input.sessionID) || "unknown";
      let items = 0;
      let chars = 0;
      let injected = false;
      let error = null;
      try {
        // Guard: inject historical context once per session. The hook
        // fires on every model request with a stable sessionID (verified);
        // the first request has no conversation history yet, so injected
        // context has maximal value there, while later requests already
        // carry accumulated turns. Re-injection would only duplicate tokens.
        // Marked only on successful push, so a failed first attempt may
        // still succeed once later in the same session.
        if (traceInjectedSessions.has(sessionID)) {
          return;
        }
        const fs = await import("node:fs/promises");
        const path = await import("node:path");
        const proc = Bun.spawnSync([
          TRACE_PYTHON,
          "-m",
          "memory.session_context",
          "--db",
          TRACE_DB,
          "--exclude-session",
          sessionID,
        ], { cwd: directory, stderr: "pipe", stdout: "pipe" });
        const block = proc.stdout ? proc.stdout.toString().trim() : "";
        let stats = {};
        try {
          stats = JSON.parse(proc.stderr ? proc.stderr.toString() : "{}");
        } catch {}
        items = stats.items || 0;
        chars = stats.chars || 0;
        if (proc.exitCode === 0 && block) {
          output.system.push(block);
          traceInjectedSessions.add(sessionID);
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
            items,
            chars,
            injected,
            latencyMs: Date.now() - started,
            error,
          });
          await fs2.appendFile(path2.join(path2.dirname(TRACE_DB), "retrieval.log"), line + "\\n");
        } catch {}
      }
    },
  };
};
"""


def render_plugin(python_exe: str, trace_db: str) -> str:
    """Render the plugin source with install-time paths baked in."""
    return (
        PLUGIN_JS_TEMPLATE
        .replace("__TRACE_PYTHON__", _js_string(python_exe))
        .replace("__TRACE_DB__", _js_string(trace_db))
    )


def install_plugin(project_path: Path) -> dict:
    """Write the project-local TRACE plugin. Returns status info."""
    project_path = project_path.resolve()
    trace_db = str(project_mod.db_path(project_path))
    python_exe = project_mod.mcp_server_command(project_path)[0]
    plugin_path = project_path / PLUGIN_DIRNAME / PLUGIN_FILENAME
    plugin_path.parent.mkdir(parents=True, exist_ok=True)
    existed = plugin_path.exists()
    plugin_path.write_text(render_plugin(python_exe, trace_db), encoding="utf-8")
    return {
        "status": "updated" if existed else "created",
        "plugin_path": str(plugin_path),
        "message": "OpenCode TRACE memory plugin installed",
    }
