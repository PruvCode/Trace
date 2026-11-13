"""Project-local task identities and references to evidence already on disk."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path
from uuid import uuid4

from memory import redact, store

MAX_EVIDENCE = 10
TASK_BINDINGS_VERSION = "task-bindings.version"


def _label(value: str, field: str, limit: int) -> str:
    if isinstance(value, str):
        value = value.strip()
        if 0 < len(value) <= limit and value.isprintable():
            return redact.redact_text(value)
    raise ValueError(f"{field} must contain 1 to {limit} printable characters")


def create_task(conn: sqlite3.Connection, title: str, subsystem: str | None = None) -> dict:
    task = {
        "id": uuid4().hex,
        "title": _label(title, "task title", 200),
        "subsystem": _label(subsystem, "subsystem", 80) if subsystem is not None else None,
    }
    store.ensure_task_schema(conn)
    with conn:
        conn.execute("INSERT INTO tasks (id, title, subsystem) VALUES (:id, :title, :subsystem)", task)
    return task


def list_tasks(conn: sqlite3.Connection, subsystem: str | None = None, limit: int = 10) -> list[dict]:
    store.ensure_task_schema(conn)
    where, params = "", ()
    if subsystem is not None:
        where, params = " WHERE subsystem = ?", (_label(subsystem, "subsystem", 80),)
    return [dict(row) for row in conn.execute(
        f"SELECT id, title, subsystem FROM tasks{where} ORDER BY subsystem, title, id LIMIT ?",
        (*params, store.clamp_limit(limit)),
    )]


def get_task(conn: sqlite3.Connection, task_id: str) -> dict:
    if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise ValueError("task ID must be a 32-character lowercase hexadecimal ID from this project")
    store.ensure_task_schema(conn)
    row = conn.execute("SELECT id, title, subsystem FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError("task ID does not exist in this project")
    return dict(row)


def link_event(conn: sqlite3.Connection, task_id: str, event_id: int) -> dict:
    get_task(conn, task_id)
    _check_row_id(event_id, "event ID")
    with conn:
        linked = conn.execute(
            "UPDATE events SET task_id = ? WHERE id = ? AND (task_id IS NULL OR task_id = ?)",
            (task_id, event_id, task_id),
        )
        if not linked.rowcount:
            row = conn.execute("SELECT task_id FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                raise ValueError("event ID does not exist in this project")
            raise ValueError("event already belongs to another task")
    return {"id": event_id, "task_id": task_id}


def bind_session(conn: sqlite3.Connection, task_id: str, session_id: str) -> dict:
    get_task(conn, task_id)
    store.ensure_capture_schema(conn)
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", session_id):
        raise ValueError("use a session ID from 'trace transcript sessions'")
    with conn:
        saved = conn.execute(
            "INSERT INTO task_sessions (transcript_id, task_id)"
            " SELECT id, ? FROM transcript_messages WHERE source_id = ? AND session_id = ? AND role = 'system'"
            " ON CONFLICT(transcript_id) DO UPDATE SET task_id = excluded.task_id",
            (task_id, f"session:{session_id}", session_id),
        )
        if not saved.rowcount:
            raise ValueError("OpenCode session has not been captured in this project")
    return {"session_id": session_id, "task_id": task_id}


def bound_task_id(conn: sqlite3.Connection, session_id: str | None) -> str | None:
    if not session_id or not store.has_session_bindings(conn):
        return None
    row = conn.execute(
        "SELECT ts.task_id FROM task_sessions ts JOIN transcript_messages m ON m.id = ts.transcript_id"
        " WHERE m.source_id = ? AND m.session_id = ? AND m.role = 'system'",
        (f"session:{session_id}", session_id),
    ).fetchone()
    return row[0] if row is not None else None


def _check_row_id(value: int, field: str) -> None:
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise ValueError(f"{field} must be a positive SQLite row ID")


def local_git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    # Ignore inherited Git repository overrides and avoid network access in partial clones.
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    env.update(GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1", GIT_ALLOW_PROTOCOL="")
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, env=env, input=data,
            capture_output=True, timeout=5, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("could not verify Git evidence in the local repository") from exc
    return result.stdout


def is_commit(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value) is not None


def is_git_path(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) <= 1024 and value.isprintable()
        and "\\" not in value and ":" not in value
        and not any(part.casefold() in ("", ".", "..", ".git") for part in value.split("/"))
    )


def task_context_file(file: str | None) -> str | None:
    if file is None:
        return None
    path = file.replace("\\", "/")
    if not is_git_path(path):
        raise ValueError("Use a repository-relative file path for task context")
    return path


def _git_file_exists(root: Path, commit: str, path: str) -> bool:
    lines = local_git(
        root, "cat-file", "--batch-check=%(objectname) %(objecttype)",
        data=f"{commit}\n{commit}:{path}\n".encode("utf-8"),
    ).splitlines()
    return (
        len(lines) == 2 and lines[0] == f"{commit} commit".encode("ascii")
        and lines[1].endswith(b" blob")
    )


def evidence_links(
    conn: sqlite3.Connection,
    evidence: list[dict] | None,
    project_root: Path | None,
) -> list[tuple[int | None, str | None, str | None]]:
    """Validate references without reading transcript text or Git file contents."""
    if evidence is None:
        return []
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
        raise ValueError(f"evidence must be a list of up to {MAX_EVIDENCE} references")
    links = {}
    for entry in evidence:
        if not isinstance(entry, dict):
            raise ValueError("each evidence reference must be an object")
        kind = entry.get("type")
        if kind == "transcript" and entry.keys() == {"type", "message_id"}:
            message_id = entry["message_id"]
            _check_row_id(message_id, "transcript message ID")
            if conn.execute("SELECT 1 FROM transcript_messages WHERE id = ?", (message_id,)).fetchone() is None:
                raise ValueError("transcript message ID does not exist in this project")
            links[(message_id, None, None)] = None
        elif kind == "git" and entry.keys() == {"type", "commit", "path"}:
            commit, path = entry["commit"], entry["path"]
            if not is_commit(commit):
                raise ValueError("Git evidence requires a full commit hash")
            if not is_git_path(path):
                raise ValueError("Git evidence requires a repository-relative file path")
            if redact.redact_text(path) != path:
                raise ValueError("Git evidence path contains sensitive text")
            if project_root is None or not (project_root / ".git").exists():
                raise ValueError("Git evidence requires the TRACE project to be a Git repository root")
            commit = commit.lower()
            link = (None, commit, path)
            if link in links:
                continue
            if not _git_file_exists(project_root, commit, path):
                raise ValueError("Git evidence file does not exist at that commit in this project")
            links[link] = None
        else:
            raise ValueError("evidence must be a transcript message ID or a Git commit and file path")
    return list(links)
