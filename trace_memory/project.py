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
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from memory import episodic as episodic_mod
from memory import git_events as git_events_mod
from memory import store as store_mod
from memory import structural as structural_mod
from memory import transcript as transcript_mod
from memory import opencode_capture as opencode_capture_mod
from memory import opencode_transparent as opencode_transparent_mod

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


def require_db(project: Path) -> Path:
    """Return the database path or raise a helpful error (exit 2)."""
    db = db_path(project)
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


def index_project(project: Path) -> dict:
    """Rebuild the structural index for an initialized project."""
    project = project.resolve()
    require_db(project)
    stats = structural_mod.index_workspace(project, db_path(project))
    return {"project": str(project), "db": str(db_path(project)), **stats}


def _counts(conn: sqlite3.Connection) -> dict:
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


def status_info(project: Path) -> dict:
    """Small dict describing local memory state (used by status + report)."""
    project = project.resolve()
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        counts = _counts(conn)
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
    return [asdict(e) for e in events]


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
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        return store_mod.find_definition(conn, name, limit)
    finally:
        conn.close()


def find_callers_of(project: Path, name: str, limit: int = 10) -> list[dict]:
    project = project.resolve()
    db = require_db(project)
    conn = store_mod.connect(db)
    try:
        return store_mod.find_callers(conn, name, limit)
    finally:
        conn.close()


def search_project_symbols(project: Path, query: str, limit: int = 10) -> list[dict]:
    project = project.resolve()
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
    return [asdict(e) for e in events]


def _git(project: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(project),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def git_summary(project: Path) -> dict:
    """Best-effort local git facts (never raises; empty when not a repo)."""
    project = project.resolve()
    if not (project / ".git").exists():
        return {"is_repo": False}
    branch = _git(project, "rev-parse", "--abbrev-ref", "HEAD")
    head = _git(project, "rev-parse", "HEAD")
    log = _git(project, "log", "--oneline", "-5")
    return {
        "is_repo": True,
        "branch": branch,
        "head": head,
        "recent": log.splitlines() if log else [],
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
    if not (project / ".git").exists():
        raise TraceUserError(
            f"not a git repository: {project}",
            "'trace history' reads git history, which needs a .git directory.",
            "Run it inside a git project, or use 'trace events' for "
            "recorded session memory instead.",
        )
    try:
        return git_events_mod.get_git_history(
            project, path=path, symbol=symbol, limit=limit
        )
    except RuntimeError as exc:
        raise TraceUserError(
            f"cannot read git history: {exc}",
            "git reported an error for this repository.",
            "Check 'git -C \"{}\" log' works, then retry.".format(project),
        ) from exc


def gitignore_hint(project: Path) -> str | None:
    """One-line reminder to keep local memory out of git, or None.

    Best-effort and never raises: returns the hint only when the project
    is a git repository but ``.agent-memory/`` is not ignored there.
    """
    project = project.resolve()
    if not (project / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", f"{MEMORY_DIRNAME}/"],
            cwd=str(project),
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return None  # already ignored
    return (
        f"note: add '{MEMORY_DIRNAME}/' to .gitignore "
        "to keep local memory out of git"
    )


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
            )
        except ValueError as exc:
            raise TraceUserError(
                f"cannot search transcripts: {exc}",
                "a filter value failed validation.",
                "Valid --role values: user, assistant, system, tool.",
            ) from exc
    finally:
        conn.close()
    return [asdict(m) for m in messages]


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
    return [asdict(m) for m in messages]


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


def mcp_server_command(project: Path) -> list[str]:
    """Command an MCP-compatible agent uses to reach this project's memory."""
    project = project.resolve()
    db = require_db(project)
    return [
        sys.executable,
        "-m",
        "memory.mcp_server",
        "--db",
        str(db),
        "--repo",
        str(project),
    ]


def mcp_config(project: Path) -> dict:
    """MCP client configuration snippet for this project's memory server."""
    return {
        "mcpServers": {
            "trace-memory": {
                "command": mcp_server_command(project),
            }
        }
    }


def mcp_config_json(project: Path) -> str:
    return json.dumps(mcp_config(project), indent=2)


def reset_project(project: Path) -> dict:
    """Delete local TRACE memory for one project (explicit only)."""
    project = project.resolve()
    mem = memory_dir(project)
    db = db_path(project)
    if not mem.exists():
        return {"project": str(project), "removed": False}
    import shutil

    shutil.rmtree(mem, ignore_errors=False)
    return {"project": str(project), "removed": True, "db_was": str(db)}


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
    project = project.resolve()
    adapter = opencode_capture_mod.create_opencode_adapter()
    results = adapter.get_recent_context(project, query, limit)
    return [
        {
            "role": r.role,
            "content": r.content,
            "timestamp": r.timestamp,
            "session_id": r.session_id,
            "metadata": r.metadata,
        }
        for r in results
    ]


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


def setup_opencode_transparent(project: Path) -> dict:
    """Set up transparent OpenCode integration (one-time setup + start monitoring)."""
    project = project.resolve()
    adapter = opencode_transparent_mod.create_opencode_transparent_adapter()
    return adapter.setup_integration(project)
