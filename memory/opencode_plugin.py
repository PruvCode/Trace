"""Project-local OpenCode plugin that injects TRACE session-start memory.

TRACE owns retrieval (memory.message_context over memory.session_context);
this module only owns the thin JavaScript glue, written to
``.trace-service/opencode-plugin.js`` and listed in the project's
``opencode.json``.

It is deliberately not in ``.opencode/plugins/``: OpenCode 1.18.32 answers that
directory with an ``npm install`` of ``@opencode-ai/plugin`` into the project,
measured at 61 MB and 86-130 s of startup delay for every project. Referenced
from config, the same install happens once per user. The plugin itself never
imports that package.

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

OpenCode 2.x does not run V1 plugins. It gets a V2 plugin directory holding the
same logic on the documented ``ctx.session.hook("context", ...)``, which also
adds to ``event.messages``: memory stays out of the trusted system prompt.
"""

from __future__ import annotations

import json
from pathlib import Path

from memory.opencode_files import INTEGRATION_DISABLED, atomic_write_text, require_local_paths
from memory.opencode_service import SERVICE_DIR_NAME, default_opencode_db_path
from memory.task_library import TASK_BINDINGS_VERSION
from trace_memory import project as project_mod

PLUGIN_FILENAME = "opencode-plugin.js"
LEGACY_PLUGIN_PATH = ".opencode/plugins/trace-memory.js"
V2_PLUGIN_DIRNAME = "opencode-plugin-v2"
PLUGIN_HEADER = "// TRACE automatic session-start memory (installed by TRACE setup)."


def _js_string(value: str) -> str:
    """Render a Python string as a JS string literal (backslash-safe)."""
    return json.dumps(value)


_TASK_CACHE_JS = """
const taskVersionPath = join(dirname(TRACE_DB), __TASK_VERSION_FILE__);
const integrationDisabled = join(dirname(dirname(TRACE_DB)), __SERVICE_DIR__, __INTEGRATION_DISABLED__);
let taskVersion = "";

function memoryUnavailable() {
  return existsSync(integrationDisabled) || !existsSync(TRACE_DB);
}

function refreshTaskBindings() {
  let version = "";
  try {
    const info = lstatSync(taskVersionPath);
    if (info.isFile() && info.size === 32) version = readFileSync(taskVersionPath, "utf8");
  } catch {}
  if (version !== taskVersion) {
    taskVersion = version;
    traceBlocks.clear();
    traceAttempts.clear();
  }
}
""".replace("__TASK_VERSION_FILE__", _js_string(TASK_BINDINGS_VERSION)).replace(
    "__SERVICE_DIR__", _js_string(SERVICE_DIR_NAME)
).replace("__INTEGRATION_DISABLED__", _js_string(INTEGRATION_DISABLED))


PLUGIN_JS_TEMPLATE = PLUGIN_HEADER + """
import { existsSync, lstatSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
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
__TRACE_TASK_CACHE__

function hasTraceBlock(messages) {
  return messages.some((m) => Array.isArray(m && m.parts) && m.parts.some(
    (p) => p && p.type === "text" && typeof p.text === "string" && p.text.startsWith(TRACE_LABEL)));
}

export const TraceMemoryPlugin = async ({ directory }) => {
  return {
    "experimental.chat.messages.transform": async (input, output) => {
      if (memoryUnavailable()) return;
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
        refreshTaskBindings();
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
              TRACE_PYTHON, "-P", "-m", "memory.message_context",
              "--db", TRACE_DB,
              "--opencode-db", OPENCODE_DB,
              "--project-dir", directory,
              "--session-id", sessionID,
              "--ensure-capture",
              "--query-stdin",
            ], { cwd: directory, stdin: Buffer.from(taskText, "utf8"),
                 stderr: "pipe", stdout: "pipe", timeout: 30000 });
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
        if (!block || memoryUnavailable()) {
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
        error = (e && e.name) || "Error";  // never the message: it can quote the task
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


PLUGIN_V2_TEMPLATE = PLUGIN_HEADER + """
// PATHS BAKED AT INSTALL TIME - regenerated on every setup_integration.
import { spawnSync } from "node:child_process";
import { appendFileSync, existsSync, lstatSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

const TRACE_PYTHON = __TRACE_PYTHON__;
const TRACE_DB = __TRACE_DB__;
const OPENCODE_DB = __OPENCODE_DB__;
const TRACE_PROJECT = __TRACE_PROJECT__;
const TRACE_LABEL = "[TRACE PROJECT MEMORY";
const TRACE_HELPER_ATTEMPTS = 2;
const traceBlocks = new Map();
const traceAttempts = new Map();
__TRACE_TASK_CACHE__

function textOf(message) {
  const content = message && message.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.filter((p) => p && typeof p.text === "string").map((p) => p.text).join(" ");
}

function helper(sessionID, task) {
  traceAttempts.set(sessionID, (traceAttempts.get(sessionID) || 0) + 1);
  const proc = spawnSync(TRACE_PYTHON, [
    "-P", "-m", "memory.message_context",
    "--db", TRACE_DB,
    "--opencode-db", OPENCODE_DB,
    "--project-dir", TRACE_PROJECT,
    "--session-id", sessionID,
    "--ensure-capture",
    "--query-stdin",
  ], { cwd: TRACE_PROJECT, input: task, encoding: "utf8", windowsHide: true, timeout: 30000 });
  const out = (proc.stdout || "").trim();
  if (out.startsWith(TRACE_LABEL)) traceBlocks.set(sessionID, out);
  else if (proc.status === 0 || traceAttempts.get(sessionID) >= TRACE_HELPER_ATTEMPTS) traceBlocks.set(sessionID, "");
}

export default {
  id: "trace-memory",
  async setup(ctx) {
    if (!ctx || !ctx.session || typeof ctx.session.hook !== "function") return;
    await ctx.session.hook("context", (event) => {
      if (memoryUnavailable() || !event || !event.sessionID || !Array.isArray(event.messages)) return;
      const started = Date.now();
      let spawned = false;
      let chars = 0;
      let error = null;
      try {
        const first = event.messages.find((m) => m && m.role === "user");
        refreshTaskBindings();
        if (!traceBlocks.has(event.sessionID)) {
          spawned = true;
          helper(event.sessionID, textOf(first).slice(0, 2000));
        }
        const block = traceBlocks.get(event.sessionID);
        if (block && !memoryUnavailable() && !event.messages.some((m) => textOf(m).startsWith(TRACE_LABEL))) {
          event.messages.unshift({ role: "user", content: [{ type: "text", text: block }] });
          chars = block.length;
        }
      } catch (e) {
        traceBlocks.set(event.sessionID, "");
        error = (e && e.name) || "Error";  // never the message: it can quote the task
      }
      try {
        appendFileSync(join(dirname(TRACE_DB), "retrieval.log"), JSON.stringify({
          ts: new Date().toISOString(), sessionID: event.sessionID, channel: "session.context",
          spawned, injected: chars > 0, chars, latencyMs: Date.now() - started, error,
        }) + "\\n");
      } catch {}
    });
  },
};
"""


def render_plugin(python_exe: str, trace_db: str, opencode_db: str) -> str:
    """Render the plugin source with install-time paths baked in."""
    return (
        PLUGIN_JS_TEMPLATE
        .replace("__TRACE_PYTHON__", _js_string(python_exe))
        .replace("__TRACE_DB__", _js_string(trace_db))
        .replace("__OPENCODE_DB__", _js_string(opencode_db))
        .replace("__TRACE_TASK_CACHE__", _TASK_CACHE_JS)
    )


def plugin_path(project_path: Path) -> Path:
    return project_path.resolve() / SERVICE_DIR_NAME / PLUGIN_FILENAME


def plugin_v2_dir(project_path: Path) -> Path:
    return project_path.resolve() / SERVICE_DIR_NAME / V2_PLUGIN_DIRNAME


def plugin_v2_entry() -> dict:
    """The ``plugins`` entry for an OpenCode 2.x opencode.json."""
    return {"package": f"./{SERVICE_DIR_NAME}/{V2_PLUGIN_DIRNAME}"}


def plugin_uri(project_path: Path) -> str:
    """The ``plugin`` entry for opencode.json."""
    return plugin_path(project_path).as_uri()


def is_trace_plugin_entry(entry: object) -> bool:
    """True for TRACE's own opencode.json entry, including one from a moved project."""
    if isinstance(entry, dict):
        return entry == plugin_v2_entry()
    return isinstance(entry, str) and entry.endswith(f"/{SERVICE_DIR_NAME}/{PLUGIN_FILENAME}")


def is_owned_plugin(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.readline(len(PLUGIN_HEADER) + 2).rstrip("\r\n") == PLUGIN_HEADER
    except (OSError, UnicodeError):
        return False


def check_install_targets(project: Path) -> None:
    targets = (plugin_path(project), plugin_v2_dir(project) / "index.js")
    require_local_paths(project, *targets)
    for target in targets:
        if ((target.parent.exists() and not target.parent.is_dir())
                or (target.exists() and not is_owned_plugin(target))):
            raise project_mod.TraceUserError(
                f"refusing to replace {target}",
                "the existing file was not installed by TRACE.",
                "Move the file aside yourself if you want TRACE to use that path.",
            )


def install_plugin(project_path: Path, opencode_db_path: Path | None = None) -> dict:
    """Write the project-local TRACE plugin, replacing an older install. Returns status info."""
    project_path = project_path.resolve()
    check_install_targets(project_path)
    trace_db = str(project_mod.db_path(project_path))
    python_exe = project_mod.mcp_server_command(project_path)[0]
    opencode_db = str(opencode_db_path or default_opencode_db_path())
    target = plugin_path(project_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    atomic_write_text(target, render_plugin(python_exe, trace_db, opencode_db))
    v2_index = plugin_v2_dir(project_path) / "index.js"
    v2_index.parent.mkdir(exist_ok=True)
    atomic_write_text(
        v2_index,
        PLUGIN_V2_TEMPLATE
        .replace("__TRACE_PYTHON__", _js_string(python_exe))
        .replace("__TRACE_DB__", _js_string(trace_db))
        .replace("__OPENCODE_DB__", _js_string(opencode_db))
        .replace("__TRACE_PROJECT__", _js_string(str(project_path)))
        .replace("__TRACE_TASK_CACHE__", _TASK_CACHE_JS),
    )
    _remove_if_ours(project_path, project_path / LEGACY_PLUGIN_PATH)
    return {
        "status": "updated" if existed else "created",
        "plugin_path": str(target),
        "message": "OpenCode TRACE memory plugin installed",
    }


def uninstall_plugin(project_path: Path) -> dict:
    """Remove the plugin only if TRACE wrote it (its header line matches)."""
    project_path = project_path.resolve()
    _remove_if_ours(project_path, project_path / LEGACY_PLUGIN_PATH)
    _remove_if_ours(project_path, plugin_v2_dir(project_path) / "index.js")
    return _remove_if_ours(project_path, plugin_path(project_path))


def _remove_if_ours(project: Path, target: Path) -> dict:
    try:
        require_local_paths(project, target)
    except project_mod.TraceUserError:
        return {"status": "kept", "plugin_path": str(target), "message": "redirected path; left in place"}
    if not target.exists():
        return {"status": "absent", "plugin_path": str(target)}
    if not is_owned_plugin(target):
        return {"status": "kept", "plugin_path": str(target),
                "message": "file was not written by TRACE; left in place"}
    target.unlink()
    for parent in (target.parent, target.parent.parent):
        try:
            parent.rmdir()  # only succeeds when empty
        except OSError:
            break
    return {"status": "removed", "plugin_path": str(target)}
