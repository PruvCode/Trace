"""Local project helpers for the ``trace`` CLI (daily-use layer).

One project = one directory containing ``.agent-memory/memory.db``.
The layout intentionally mirrors ``memory.reference`` (the benchmark
backend), so a database created by ``trace init`` is the same artifact the
reference backend and the MCP server operate on:

- ``<project>/.agent-memory/memory.db`` — SQLite + FTS5, created locally.
- No network, no accounts, no telemetry. Everything stays on disk here.

No benchmark imports: this module only uses ``memory.*``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from memory import captured_outcomes
from memory import entity_graph
from memory import episodic as episodic_mod
from memory import evidence as evidence_mod
from memory import git_events as git_events_mod
from memory import store as store_mod
from memory import session_context as session_context_mod
from memory import structural as structural_mod
from memory import task_library
from memory import transcript as transcript_mod
from memory import opencode_capture as opencode_capture_mod
from memory import opencode_transparent as opencode_transparent_mod
from memory import opencode_service as opencode_service_mod

# Must stay identical to memory.reference.MEMORY_DIRNAME / DB_FILENAME so the
# CLI, the MCP server, and the benchmark backend share one database layout.
MEMORY_DIRNAME = ".agent-memory"
DB_FILENAME = "memory.db"
TRANSCRIPT_FILENAMES = ("opencode_transcript.jsonl", "transcript.jsonl")


class TraceUserError(RuntimeError):
    """An expected user mistake (bad path, missing db, invalid event).

    Carries WHAT / WHY / HOW so the CLI can print a helpful message and
    exit 2 without a stack trace.
    """

    def __init__(self, what: str, why: str, how: str) -> None:
        super().__init__(what)
        self.what = what
        self.why = why
        self.how = how


def resolve_project(path: str | Path | None) -> Path:
    """Resolve the project directory (default: current working directory)."""
    candidate = Path(path).expanduser() if path else Path.cwd()
    resolved = candidate.resolve()
    if not resolved.exists():
        raise TraceUserError(
            f"project not found: {candidate}",
            "the path does not exist.",
            "Run 'trace init' inside an existing project directory, "
            "or pass a valid path: trace init <path>.",
        )
    if not resolved.is_dir():
        raise TraceUserError(
            f"not a directory: {resolved}",
            "'trace' works on project directories.",
            "Pass the project directory itself, not a file inside it.",
        )
    return resolved


def db_path(project: Path) -> Path:
    return project.resolve() / MEMORY_DIRNAME / DB_FILENAME


def memory_dir(project: Path) -> Path:
    return project.resolve() / MEMORY_DIRNAME


def is_initialized(project: Path) -> bool:
    return db_path(project).exists()


def require_local_db(project: Path) -> Path:
    return store_mod.require_local_storage(project.resolve(), db_path(project))


def require_db(project: Path) -> Path:
    """Return the database path or raise a helpful error (exit 2)."""
    db = require_local_db(project)
    if not db.exists():
        raise TraceUserError(
            f"no TRACE memory here: {project}",
            "this project has no .agent-memory/memory.db yet.",
            f"Run 'trace init \"{project}\"' first to create local memory.",
        )
    return db


def init_project(project: Path) -> dict:
    """Create (or re-create) local memory: mkdir + structural index.

    Idempotent: safe to re-run; re-indexing rebuilds structural rows
    deterministically while keeping recorded episodic events.
    """
    project = project.resolve()
    db = db_path(project)
    stats = structural_mod.index_workspace(project, db)
    return {
        "project": str(project),
        "db": str(db),
        "initialized": True,
        **stats,
    }


def ensure_structural_memory(project: Path) -> dict:
    """Ensure structural memory exists and is fresh.

    - If no memory.db: creates it and builds structural index
    - If memory exists but stale: rebuilds structural index
    - If fresh: returns status without rebuilding
    """
    return structural_mod.ensure_structural_memory(project)


def index_project(project: Path) -> dict:
    """Rebuild the structural index for an initialized project."""
    project = project.resolve()
    require_db(project)
    stats = structural_mod.index_workspace(project, db_path(project))
    return {"project": str(project), "db": str(db_path(project)), **stats}


def memory_counts(conn: sqlite3.Connection) -> dict:
    symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    relationships = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
    files = conn.execute("SELECT COUNT(DISTINCT file) FROM symbols").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    transcripts = conn.execute("SELECT COUNT(*) FROM transcript_messages").fetchone()[0]
    return {
        "files": files,
        "symbols": symbols,
        "relationships": relationships,
        "events": events,
        "transcripts": transcripts,
    }


def _events_by_type(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT type, COUNT(*) FROM events GROUP BY type ORDER BY type"
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _events_by_source(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT source, COUNT(*) FROM events GROUP BY source ORDER BY source"
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def privacy_info(project: Path) -> dict:
    """Report project-local capture and recall settings without reading transcript text."""
    from memory.opencode_files import integration_marker, require_local_paths
    from memory import opencode_plugin
    from trace_memory import native

    database = require_local_db(project)
    marker = integration_marker(project)
    legacy = opencode_plugin.plugin_path(project)
    modern = opencode_plugin.plugin_v2_dir(project) / "index.js"
    require_local_paths(project, database, marker, legacy, modern)
    initialized = database.is_file()
    installed = any(path.is_file() and opencode_plugin.is_owned_plugin(path) for path in (legacy, modern))
    disabled = os.path.lexists(marker)
    test_capture = False
    if initialized:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'test_capture_policy'"
            ).fetchone()
            if table:
                test_capture = db.execute("SELECT 1 FROM test_capture_policy WHERE id = 1").fetchone() is not None
    capture = opencode_service_mod.get_service(project)
    capture.check_storage_paths()
    pause_file = project / opencode_service_mod.SERVICE_DIR_NAME / opencode_service_mod.DISABLED_FILE
    if pause_file.is_file():
        capture_state = "paused"
    elif (pause_file.parent / opencode_service_mod.PID_FILE).is_file():
        capture_state = capture.status(cleanup_stale=False)["status"]
    else:
        capture_state = "stopped"
    recall_state = "disabled" if disabled else ("plugin installed" if initialized and installed else "not configured")
    hosts = {"opencode": {"capture": capture_state, "recall": recall_state, "enrolled": installed}}
    for host in native.HOOK_HOSTS:
        try:
            hosts[host] = native.privacy_state(project, host)
        except (OSError, ValueError, TraceUserError):
            hosts[host] = {"capture": "error", "recall": "error", "enrolled": False}
    return {
        "database": str(database),
        "initialized": initialized,
        "capture": capture_state,
        "automatic_recall": recall_state,
        "hosts": hosts,
        "selected_test_capture_opt_in": test_capture,
    }


def preview_context(project: Path, session_id: str | None, query: str | None, *, agent: str = "opencode") -> str:
    """Render the block TRACE would assemble for a new request."""
    from memory import synthetic
    from memory.opencode_files import integration_marker, require_local_paths

    database = require_local_db(project)
    if agent != "opencode":
        from memory import dialogue
        from trace_memory import native

        if not native.hook_enabled(project, agent) or not database.is_file():
            return ""
        session = session_id or ""
        if session and not session.startswith(f"{agent}:"):
            session = f"{agent}:{session}"
        return dialogue.recall(database, session, query or "")
    marker = integration_marker(project)
    require_local_paths(project, database, marker)
    if marker.exists():
        return ""
    task_aware = os.environ.get("TRACE_TASK_AWARE") != "0"
    result = session_context_mod.get_session_start_context(
        database, project_root=project, exclude_session_id=session_id,
        query=query if task_aware else None, use_bound_task=task_aware, notes_first=True,
    )
    return result.get("notes") or synthetic.render_user_block(result["items"])


def status_info(project: Path) -> dict:
    """Small dict describing local memory state (used by status + report)."""
    project = project.resolve()
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        counts = memory_counts(conn)
        by_type = _events_by_type(conn)
        by_source = _events_by_source(conn)
    finally:
        conn.close()
    return {
        "project": str(project),
        "db": str(db),
        "db_bytes": db.stat().st_size,
        **counts,
        "events_by_type": by_type,
        "events_by_source": by_source,
    }


def recent_events(project: Path, limit: int = 10) -> list[dict]:
    project = project.resolve()
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        events = episodic_mod.search_events(conn, limit=store_mod.clamp_limit(limit))
    finally:
        conn.close()
    return [e.as_record() for e in events]


@contextmanager
def task_database(project: Path):
    conn = store_mod.connect(require_db(project))
    try:
        yield conn
    except ValueError as exc:
        raise TraceUserError(
            str(exc), "the task or event could not be selected or saved.",
            "Run 'trace task list' or 'trace events' to find IDs for this project.",
        ) from exc
    finally:
        conn.close()


def task_details(conn: sqlite3.Connection, task_id: str, limit: int) -> dict:
    store_mod.ensure_evidence_schema(conn)
    task = task_library.get_task(conn, task_id)
    task["events"] = [event.as_record() for event in episodic_mod.search_events(conn, task_id=task_id, limit=limit)]
    return task


def invalidate_context_cache(project: Path) -> bool:
    from memory.opencode_files import atomic_write_text, require_local_paths

    target = memory_dir(project) / task_library.TASK_BINDINGS_VERSION
    try:
        require_local_paths(project, target)
        atomic_write_text(target, uuid4().hex)
    except (OSError, TraceUserError):
        return False
    return True


def bind_task_session(project: Path, conn: sqlite3.Connection, task_id: str, session_id: str) -> dict:
    result = task_library.bind_session(conn, task_id, session_id)
    if not invalidate_context_cache(project):
        result["restart_opencode_to_refresh"] = True
    return result


def task_context(project: Path, task_id: str, query: str, limit: int, *, file: str | None = None) -> str:
    with task_database(project) as conn:
        task_library.get_task(conn, task_id)
    return session_context_mod.get_session_start_context(
        require_db(project), project_root=project, task_id=task_id, query=query, max_items=store_mod.clamp_limit(limit),
        file=task_library.task_context_file(file),
    )["text"]


def expand_evidence(project: Path, reference: str, task_id: str | None, offset: int) -> str:
    with task_database(project) as conn:
        return evidence_mod.expand(conn, project, reference, task_id=task_id, offset=offset)


def test_capture_policy(project: Path, action: str) -> dict:
    with closing(store_mod.connect(require_db(project))) as conn:
        return captured_outcomes.configure(conn, action)


def record_user_event(
    project: Path,
    *,
    type: str,
    message: str | None = None,
    payload: dict | None = None,
    repo: str | None = None,
    file: str | None = None,
    symbol: str | None = None,
    commit: str | None = None,
    task_id: str | None = None,
    evidence: list[dict] | None = None,
) -> dict:
    """Record one episodic event from the CLI. Returns {'id': ...}."""
    project = project.resolve()
    db = require_db(project)
    merged: dict = dict(payload or {})
    if message:
        merged.setdefault("message", message)
    label = repo.strip() if repo and repo.strip() else project.name
    conn = store_mod.connect(db)
    try:
        try:
            event_id = episodic_mod.record_event(
                conn,
                type=type,
                source="agent",
                repo=label,
                commit=commit,
                file=file,
                symbol=symbol,
                payload=merged,
                task_id=task_id,
                evidence=evidence,
                project_root=project,
            )
        except (ValueError, TypeError) as exc:
            raise TraceUserError(
                f"cannot record event: {exc}",
                "the event fields failed validation.",
                "Valid types: investigation, attempt, decision, observation, "
                "git_change. Payload must be JSON, e.g. "
                "--payload '{\"finding\": \"...\"}'.",
            ) from exc
    finally:
        conn.close()
    return {"id": event_id, "db": str(db)}


def find_definitions(project: Path, name: str, limit: int = 10) -> list[dict]:
    project = project.resolve()
    # Ensure structural memory is fresh before querying
    structural_mod.ensure_structural_memory(project)
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        return store_mod.find_definition(conn, name, limit)
    finally:
        conn.close()


def code_graph(project: Path, node_id: str | None, name: str | None, limit: int, offset: int) -> dict:
    project = project.resolve()
    ensure_structural_memory(project)
    with closing(store_mod.connect(require_db(project))) as conn:
        conn.execute("BEGIN")
        result = entity_graph.query(conn, node_id, name, limit, offset)
        if "node" in result:
            from memory import entity_history

            result["history"] = entity_history.neighborhood(conn, project, result["node"]["id"])
        return result


def find_callers_of(project: Path, name: str, limit: int = 10) -> list[dict]:
    project = project.resolve()
    structural_mod.ensure_structural_memory(project)
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        return store_mod.find_callers(conn, name, limit)
    finally:
        conn.close()


def search_project_symbols(project: Path, query: str, limit: int = 10) -> list[dict]:
    project = project.resolve()
    structural_mod.ensure_structural_memory(project)
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        return store_mod.search_symbols(conn, query, limit)
    finally:
        conn.close()


def search_project_events(
    project: Path,
    *,
    type: str | None = None,
    query: str | None = None,
    symbol: str | None = None,
    file: str | None = None,
    commit: str | None = None,
    limit: int = 10,
    task_id: str | None = None,
) -> list[dict]:
    project = project.resolve()
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        try:
            events = episodic_mod.search_events(
                conn,
                type=type,
                query=query,
                symbol=symbol,
                file=file,
                commit=commit,
                limit=limit,
                task_id=task_id,
                natural=True,
            )
        except ValueError as exc:
            raise TraceUserError(
                f"cannot search events: {exc}",
                "a filter value failed validation.",
                "Valid --type values: investigation, attempt, decision, "
                "observation, git_change.",
            ) from exc
    finally:
        conn.close()
    return [e.as_record() for e in events]


def _git(project: Path, *args: str) -> str | None:
    try:
        return task_library.local_git(project, *args).decode("utf-8", errors="replace").strip()
    except ValueError:
        return None


def git_summary(project: Path) -> dict:
    """Best-effort local git facts (never raises; empty when not a repo)."""
    project = project.resolve()
    scope = task_library.git_scope(project)
    if scope is None:
        return {"is_repo": False}
    branch = _git(scope.root, "symbolic-ref", "--short", "HEAD")
    head = _git(scope.root, "rev-parse", "--verify", "HEAD")
    error = None
    try:
        if scope.prefix:
            recent = git_events_mod.scoped_log(scope)
        else:
            log = _git(scope.root, "log", "--oneline", "-5", "HEAD")
            recent = log.splitlines() if log else []
    except RuntimeError as exc:
        recent, error = [], str(exc)
    return {
        "is_repo": True,
        "branch": branch,
        "head": head,
        "recent": recent,
        "error": error,
    }


def project_history(
    project: Path,
    *,
    path: str | None = None,
    symbol: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Deterministic git facts via the existing git_events module."""
    project = project.resolve()
    try:
        return git_events_mod.get_git_history(
            project, path=path, symbol=symbol, limit=limit
        )
    except (ValueError, RuntimeError) as exc:
        raise TraceUserError(
            f"cannot read git history: {exc}",
            "History needs a local Git worktree and a valid project-relative path.",
            "Check 'git -C \"{}\" status', then retry.".format(project),
        ) from exc


def gitignore_hint(project: Path) -> str | None:
    """One-line reminder to keep local memory out of git, or None.

    Report tracked memory and missing local excludes without editing Git.
    """
    project = project.resolve()
    scope = task_library.git_scope(project)
    if scope is None:
        return None
    folders = (MEMORY_DIRNAME, opencode_service_mod.SERVICE_DIR_NAME)
    paths = tuple(scope.object_path(folder) + "/" for folder in folders)
    hints = []
    if _git(scope.root, "--literal-pathspecs", "ls-files", "--cached", "--", *paths):
        hints.append("TRACE data is already tracked or staged. Review the Git index before sharing.")
    missing = [path for path in paths if not _git(scope.root, "check-ignore", "--no-index", "--", path)]
    if missing:
        exclude_file = _git(scope.root, "rev-parse", "--git-path", "info/exclude") or ".git/info/exclude"
        hints.append(f"Add {', '.join('/' + path for path in missing)} to {exclude_file} from the Git worktree root.")
    return " ".join(hints) or None


def transcript_files(project: Path) -> list[str]:
    """Agent transcript files present in .agent-memory (OpenCode runs only)."""
    found = []
    mem = memory_dir(project.resolve())
    for name in TRANSCRIPT_FILENAMES:
        if (mem / name).exists():
            found.append(str(mem / name))
    return found


def record_transcript_message(
    project: Path,
    *,
    session_id: str,
    role: str,
    content: str,
    timestamp: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Record one transcript message from the CLI. Returns {'id': ...}."""
    project = project.resolve()
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        try:
            msg_id = transcript_mod.record_message(
                conn,
                session_id=session_id,
                role=role,
                content=content,
                timestamp=timestamp,
                metadata=metadata,
            )
        except (ValueError, TypeError) as exc:
            raise TraceUserError(
                f"cannot record transcript message: {exc}",
                "the message fields failed validation.",
                "Valid roles: user, assistant, system, tool. "
                "Metadata must be JSON, e.g. "
                "--metadata '{\"model\": \"gpt-4\"}'.",
            ) from exc
    finally:
        conn.close()
    return {"id": msg_id, "db": str(db)}


def search_transcript_messages(
    project: Path,
    *,
    session_id: str | None = None,
    role: str | None = None,
    query: str | None = None,
    limit: int = 10,
) -> list[dict]:
    project = project.resolve()
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        try:
            messages = transcript_mod.search_messages(
                conn,
                session_id=session_id,
                role=role,
                query=query,
                limit=limit,
                natural=True,
            )
        except ValueError as exc:
            raise TraceUserError(
                f"cannot search transcripts: {exc}",
                "a filter value failed validation.",
                "Valid --role values: user, assistant, system, tool.",
            ) from exc
    finally:
        conn.close()
    return [m.as_record() for m in messages]


def get_transcript_session(
    project: Path,
    session_id: str,
    limit: int = 50,
) -> list[dict]:
    project = project.resolve()
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        try:
            messages = transcript_mod.get_session_messages(
                conn, session_id, limit
            )
        except ValueError as exc:
            raise TraceUserError(
                f"cannot get transcript session: {exc}",
                "session_id must be a non-empty string.",
                "Use 'trace transcript sessions' to list available sessions.",
            ) from exc
    finally:
        conn.close()
    return [m.as_record() for m in messages]


def list_transcript_sessions(
    project: Path,
    limit: int = 50,
) -> list[dict]:
    project = project.resolve()
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        return transcript_mod.list_sessions(conn, limit)
    finally:
        conn.close()


def transcript_count(project: Path) -> int:
    project = project.resolve()
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        return transcript_mod.count_messages(conn)
    finally:
        conn.close()


def mcp_server_command(project: Path, *, opencode: bool = False) -> list[str]:
    """Command an MCP-compatible agent uses to reach this project's memory."""
    project = project.resolve()
    db = require_db(project)
    command = [
        sys.executable,
        "-P",  # never import from the project: its queue/ or json.py must not shadow the stdlib
        "-m",
        "memory.mcp_server",
        "--evidence",
        "--db",
        str(db),
        "--repo",
        str(project),
    ]
    if opencode:
        from memory.opencode_files import integration_marker

        command.extend(("--integration-marker", str(integration_marker(project))))
    return command


def mcp_config(project: Path) -> dict:
    """MCP client configuration snippet for this project's memory server."""
    from trace_memory.integrations import mcp_entry

    project = project.resolve()
    require_db(project)
    return {
        "mcpServers": {
            "trace-memory": mcp_entry(project),
        }
    }


def mcp_config_json(project: Path) -> str:
    return json.dumps(mcp_config(project), indent=2)


def backup_project(project: Path, destination: Path) -> Path:
    """Publish a complete SQLite snapshot at a new filename."""
    import os
    import tempfile
    import time
    from contextlib import closing

    source = require_db(project)
    target = destination.expanduser().absolute()
    deadline = time.monotonic() + 30

    def check_progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("backup exceeded 30 seconds")

    staged = None
    try:
        if os.path.lexists(target):
            raise FileExistsError("destination already exists")
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".trace-backup-", delete=False) as file:
            staged = Path(file.name)
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(staged)) as dst:
                src.backup(dst, pages=256, progress=check_progress)
        if os.name == "nt":
            os.rename(staged, target)  # Windows rename refuses an existing target.
        else:
            os.link(staged, target)  # Publish without clobbering a concurrent writer.
    except (OSError, sqlite3.Error) as exc:
        raise TraceUserError(
            f"could not back up to {target}",
            str(exc),
            "Use a new filename in a private local directory. Stop capture and retry if the database is busy.",
        ) from exc
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return target


def reset_project(project: Path) -> dict:
    """Delete local TRACE memory for one project after capture has stopped."""
    import shutil

    from memory.opencode_files import atomic_write_text, integration_marker, require_local_paths
    from trace_memory import native

    project = project.resolve()
    mem = memory_dir(project)
    db = db_path(project)
    marker = integration_marker(project)
    require_local_paths(project, mem, db, marker)
    service = opencode_service_mod.get_service(project).stop(disable=True)
    if service["status"] not in ("stopped", "not_running"):
        raise TraceUserError(
            "could not reset project memory",
            service.get("message", "capture has not stopped"),
            "Wait for capture to stop and run reset again.",
        )
    try:
        with native.capture_lock(project, wait=5):
            lock = opencode_service_mod.LockHandle(
                project / opencode_service_mod.SERVICE_DIR_NAME / opencode_service_mod.LOCK_FILE,
            )
            if not lock.acquire():
                raise TimeoutError("the OpenCode capture lock is in use")
            try:
                require_local_paths(project, mem, db, marker)
                native.pause_enrolled_hosts(project)
                atomic_write_text(marker, "disabled by TRACE reset\n")
                if not mem.exists():
                    return {"project": str(project), "removed": False, "service": service}
                shutil.rmtree(mem, ignore_errors=False)
            finally:
                lock.release()
    except (OSError, ValueError) as exc:
        raise TraceUserError("could not reset project memory", str(exc),
                             "Wait for capture or memory access to stop and run reset again.") from exc
    return {"project": str(project), "removed": True, "db_was": str(db), "service": service}


def teardown_opencode(project: Path) -> dict:
    """Reverse OpenCode setup: stop capture, remove plugin + MCP entry; keep memory."""
    project = project.resolve()
    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    return adapter.teardown_integration(project)


def setup_opencode_integration(project: Path) -> dict:
    """Set up OpenCode integration for automatic capture."""
    project = project.resolve()
    adapter = opencode_capture_mod.create_opencode_adapter()
    return adapter.setup_integration(project)


def capture_opencode_session(
    project: Path,
    prompt: str,
    model: str | None = None,
    session_id: str | None = None,
) -> list[dict]:
    """Capture an OpenCode session and return captured items."""
    project = project.resolve()
    adapter = opencode_capture_mod.create_opencode_adapter()
    captured = []
    for item in adapter.capture_session(project, prompt, model, session_id):
        if isinstance(item, opencode_capture_mod.CapturedMessage):
            captured.append({
                "type": "message",
                "role": item.role,
                "content": item.content,
                "timestamp": item.timestamp,
                "session_id": item.session_id,
                "metadata": item.metadata,
            })
        elif isinstance(item, opencode_capture_mod.CapturedEvent):
            captured.append({
                "type": "event",
                "event_type": item.type,
                "content": item.content,
                "symbol": item.symbol,
                "file": item.file,
                "timestamp": item.timestamp,
                "session_id": item.session_id,
                "metadata": item.metadata,
            })
    return captured


def get_recent_context(
    project: Path,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """Get relevant historical context for a new session."""
    database = require_local_db(project)
    if not database.is_file():
        return []
    with closing(store_mod.connect(database)) as conn:
        return session_context_mod.search_context(conn, query, limit)


def start_opencode_monitoring(project: Path) -> dict:
    """Start transparent OpenCode monitoring for a project."""
    project = project.resolve()
    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    return adapter.start_monitoring(project)


def stop_opencode_monitoring(project: Path) -> dict:
    """Stop transparent OpenCode monitoring for a project."""
    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    return adapter.stop_monitoring(project)


def get_opencode_monitoring_status(project: Path) -> dict:
    """Get the status of OpenCode monitoring for a project."""
    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    return adapter.get_monitoring_status(project)


def setup_opencode_transparent(
    project: Path, *, major: int | None = None, preserve_pause: bool = False,
) -> dict:
    """Set up transparent OpenCode integration (one-time setup + start monitoring)."""
    project = project.resolve()
    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    return adapter.setup_integration(project, major=major, preserve_pause=preserve_pause)


# Persistent service functions
def start_opencode_service(project: Path) -> dict:
    """Start the persistent OpenCode capture service."""
    project = project.resolve()
    service = opencode_service_mod.get_service(project)
    return service.start()


def stop_opencode_service(project: Path) -> dict:
    """Stop the persistent OpenCode capture service."""
    project = project.resolve()
    service = opencode_service_mod.get_service(project)
    return service.stop()


def get_opencode_service_status(project: Path) -> dict:
    """Get the status of the persistent OpenCode capture service."""
    project = project.resolve()
    service = opencode_service_mod.get_service(project)
    return service.status()


def restart_opencode_service(project: Path) -> dict:
    """Restart the persistent OpenCode capture service."""
    project = project.resolve()
    service = opencode_service_mod.get_service(project)
    return service.restart()
