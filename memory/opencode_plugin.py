"""Project-local OpenCode plugin that injects TRACE session-start memory.

TRACE owns retrieval (memory.message_context over memory.session_context);
this module only owns the thin JavaScript glue installed into
``.opencode/plugins/trace-memory.js``.

Verified against OpenCode 1.18.34 with an isolated data directory:

- ``experimental.chat.messages.transform`` fires on EVERY model request of a
  session (one per agent step), with ``input = {}``
- each message in ``output.messages`` carries ``info.sessionID``, so the
  current session needs no guessing
- a message unshifted by the hook is NOT persisted: it is absent from the
  next request's ``output.messages``

So the plugin:

- reads the session id from the messages themselves
- runs ``python -m memory.message_context`` once per session (catching up
  and restarting capture first), caching the block in memory; a helper
  crash or non-zero exit is retried at most once more, while a clean
  run that injects nothing is final (never retried)
- injects that block on every model request of the session: the injected
  synthetic message is never persisted, so injecting only once would drop
  the memory from every later agent step (e.g. right after a tool call)
  and change the prompt prefix between requests; a request that already
  carries a block never gets a second copy
- appends a content-free JSON diagnostic line to
  ``.agent-memory/retrieval.log``
- fails open on any error: OpenCode must keep working without memory

No SQLite in JS. No npm dependencies. No network.
"""

from __future__ import annotations

import json
from pathlib import Path

from memory.opencode_service import default_opencode_db_path
from trace_memory import project as project_mod

PLUGIN_FILENAME = "trace-memory.js"
PLUGIN_DIRNAME = ".opencode/plugins"
PLUGIN_HEADER = "// TRACE automatic session-start memory (installed by TRACE setup)."


def _js_string(value: str) -> str:
    """Render a Python string as a JS string literal (backslash-safe)."""
    return json.dumps(value)


PLUGIN_JS_TEMPLATE = PLUGIN_HEADER + """
// PATHS BAKED AT INSTALL TIME - regenerated on every setup_integration.
const TRACE_PYTHON = __TRACE_PYTHON__;
const TRACE_DB = __TRACE_DB__;
const OPENCODE_DB = __OPENCODE_DB__;
const TRACE_LABEL = "[TRACE PROJECT MEMORY";

// sessionID -> memory block ("" when there is nothing to inject).
const traceBlocks = new Map();
// sessionID -> helper spawn attempts used. A helper that runs and reports
// "nothing to inject" (exit 0 + stats) is final, never retried. A crash or
// non-zero exit is recoverable and may be retried at most ONCE more, so a
// transient failure cannot permanently disable memory for a session while
// still bounding spawns when the helper is broken for good.
const traceAttempts = new Map();
const TRACE_HELPER_ATTEMPTS = 2;

function hasTraceBlock(messages) {
  return messages.some((m) => Array.isArray(m && m.parts) && m.parts.some(
    (p) => p && p.type === "text" && typeof p.text === "string" && p.text.startsWith(TRACE_LABEL)));
}

export const TraceMemoryPlugin = async ({ directory }) => {
  return {
    "experimental.chat.messages.transform": async (input, output) => {
      const started = Date.now();
      let sessionID = null;
      let spawned = false;
      let injected = false;
      let items = 0;
      let chars = 0;
      let reason = null;
      let capture = null;
      let error = null;
      try {
        if (!output || !Array.isArray(output.messages)) {
          reason = "no-messages";
          return;
        }
        const owner = output.messages.find((m) => m && m.info && m.info.sessionID);
        // The new session's own request text drives task-aware retrieval.
        const userMsg = output.messages.find((m) => m && m.info && m.info.role === "user");
        const taskText = userMsg && Array.isArray(userMsg.parts)
          ? userMsg.parts.filter((p) => p && p.type === "text" && !p.synthetic && typeof p.text === "string")
              .map((p) => p.text).join(" ").slice(0, 2000)
          : "";
        sessionID = owner ? owner.info.sessionID : null;
        if (!sessionID) {
          reason = "no-session-id";
          return;
        }
        if (!traceBlocks.has(sessionID)) {
          if ((traceAttempts.get(sessionID) || 0) >= TRACE_HELPER_ATTEMPTS) {
            reason = "helper-failed";
            return;
          }
          spawned = true;
          traceAttempts.set(sessionID, (traceAttempts.get(sessionID) || 0) + 1);
          let proc = null;
          try {
            proc = Bun.spawnSync([
              TRACE_PYTHON, "-m", "memory.message_context",
              "--db", TRACE_DB,
              "--opencode-db", OPENCODE_DB,
              "--project-dir", directory,
              "--session-id", sessionID,
              "--ensure-capture",
              ...(taskText ? ["--query", taskText] : []),
            ], { cwd: directory, stderr: "pipe", stdout: "pipe" });
          } catch (spawnError) {
            reason = "spawn-failed";
          }
          const out = proc && proc.stdout ? proc.stdout.toString().trim() : "";
          let stats = {};
          try {
            stats = JSON.parse(proc && proc.stderr ? proc.stderr.toString() : "{}");
          } catch {}
          reason = reason || stats.reason || null;
          capture = stats.capture || null;
          items = stats.items || 0;
          const exitCode = proc && typeof proc.exitCode === "number" ? proc.exitCode : 1;
          if (out.startsWith(TRACE_LABEL)) {
            traceBlocks.set(sessionID, out);
            traceAttempts.delete(sessionID);
          } else if (exitCode === 0 && stats.reason !== undefined) {
            // Helper ran and deliberately injected nothing (fail-open reason):
            // a final result for this session, not a retryable failure.
            traceBlocks.set(sessionID, "");
            traceAttempts.delete(sessionID);
          } else if ((traceAttempts.get(sessionID) || 0) >= TRACE_HELPER_ATTEMPTS) {
            traceBlocks.set(sessionID, "");
            reason = "helper-failed";
          }
          // else: nothing cached yet, so the next request retries once.
        }
        const block = traceBlocks.get(sessionID);
        if (!block) {
          return;
        }
        if (hasTraceBlock(output.messages)) {
          reason = "already-present";
          return;
        }
        output.messages.unshift({
          info: { role: "user", sessionID },
          parts: [{ type: "text", synthetic: true, text: block }],
        });
        injected = true;
        chars = block.length;
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
            spawned,
            injected,
            items,
            chars,
            capture,
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


def plugin_path(project_path: Path) -> Path:
    return project_path.resolve() / PLUGIN_DIRNAME / PLUGIN_FILENAME


def install_plugin(project_path: Path, opencode_db_path: Path | None = None) -> dict:
    """Write the project-local TRACE plugin. Returns status info."""
    project_path = project_path.resolve()
    trace_db = str(project_mod.db_path(project_path))
    python_exe = project_mod.mcp_server_command(project_path)[0]
    opencode_db = str(opencode_db_path or default_opencode_db_path())
    target = plugin_path(project_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    target.write_text(render_plugin(python_exe, trace_db, opencode_db), encoding="utf-8")
    return {
        "status": "updated" if existed else "created",
        "plugin_path": str(target),
        "message": "OpenCode TRACE memory plugin installed",
    }


def uninstall_plugin(project_path: Path) -> dict:
    """Remove the plugin only if TRACE wrote it (its header line matches)."""
    target = plugin_path(project_path)
    if not target.exists():
        return {"status": "absent", "plugin_path": str(target)}
    try:
        first_line = target.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        first_line = ""
    if first_line != PLUGIN_HEADER:
        return {"status": "kept", "plugin_path": str(target),
                "message": "file was not written by TRACE; left in place"}
    target.unlink()
    for parent in (target.parent, target.parent.parent):
        try:
            parent.rmdir()  # only succeeds when empty
        except OSError:
            break
    return {"status": "removed", "plugin_path": str(target)}
