"""Command endpoints for project-scoped host hooks and stdio MCP."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from memory.opencode_files import atomic_write_text, require_local_paths
from memory.opencode_service import LockHandle, SERVICE_DIR_NAME
from trace_memory.project import TraceUserError, db_path

HOOK_HOSTS = ("codex", "claude", "antigravity")
MAX_PAYLOAD_BYTES = 256 * 1024


def receipt_path(project: Path, host: str) -> Path:
    return project / SERVICE_DIR_NAME / f"integration-{host}.json"


def require_local_db(project: Path) -> Path:
    db = db_path(project)
    paths = (db, *(db.with_name(db.name + suffix) for suffix in ("-wal", "-shm", "-journal")))
    require_local_paths(project, *paths)
    for path in paths:
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("TRACE memory must use regular project-local files")
    return db


def active(project: Path, host: str) -> bool:
    receipt = receipt_path(project, host)
    require_local_paths(project, receipt)
    if not receipt.is_file() or receipt.stat().st_nlink != 1:
        return False
    with receipt.open("rb") as handle:
        raw = handle.read(MAX_PAYLOAD_BYTES + 1)
    if len(raw) > MAX_PAYLOAD_BYTES:
        return False
    data = json.loads(raw)
    return (isinstance(data, dict) and data.get("host") == host
            and data.get("project") == str(project) and data.get("phase") == "installed")


def _pause_path(project: Path, host: str) -> Path:
    path = receipt_path(project, host).with_name(f"native-{host}.paused")
    require_local_paths(project, path)
    return path


def host_available(project: Path, host: str) -> bool:
    return active(project, host) and not _pause_path(project, host).exists()


def hook_enabled(project: Path, host: str) -> bool:
    return os.environ.get("TRACE_DISABLE_HOOKS") != "1" and host_available(project, host)


@contextmanager
def capture_lock(project: Path, *, wait: float = 3):
    path = project / SERVICE_DIR_NAME / "native-capture.lock"
    require_local_paths(project, path)
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("native capture lock must be a regular project-local file")
    lock = LockHandle(path)
    if not lock.acquire(wait=wait):
        raise TimeoutError("native capture lock is busy")
    try:
        yield
    finally:
        lock.release()


def _set_pause(project: Path, host: str, paused: bool) -> None:
    path = _pause_path(project, host)
    if paused:
        atomic_write_text(path, "paused by TRACE\n")
    else:
        path.unlink(missing_ok=True)


def pause_enrolled_hosts(project: Path) -> None:
    """Pause enrolled hooks before reset while the caller holds capture_lock."""
    for host in HOOK_HOSTS:
        receipt = receipt_path(project, host)
        require_local_paths(project, receipt)
        if receipt.is_file():
            _set_pause(project, host, True)


def _diagnostic_path(project: Path, host: str, status: str) -> Path:
    path = receipt_path(project, host).with_name(f"native-{host}-{status}.json")
    require_local_paths(project, path)
    return path


def _diagnostic(project: Path, host: str, status: str, error: str | None = None) -> None:
    try:
        path = _diagnostic_path(project, host, status)
        record = {"status": status, "time": datetime.now(timezone.utc).isoformat(), "error": error}
        atomic_write_text(path, json.dumps(record) + "\n")
    except (OSError, ValueError, TraceUserError) as exc:
        print(f"TRACE native diagnostics unavailable ({type(exc).__name__})", file=sys.stderr)


def _read_diagnostic(project: Path, host: str, status: str) -> dict | None:
    try:
        path = _diagnostic_path(project, host, status)
        if not path.is_file() or path.stat().st_nlink != 1:
            return None
        with path.open("rb") as handle:
            row = json.loads(handle.read(1024))
        time = datetime.fromisoformat(row["time"]).isoformat()
        error = row["error"]
        if error not in (None, "database_busy", "database_error", "capture_lock"):
            return None
        return {"status": status, "time": time, "error": error}
    except (OSError, ValueError, KeyError, TypeError, TraceUserError):
        return None


def privacy_state(project: Path, host: str) -> dict:
    enrolled = active(project, host)
    if not enrolled:
        state = "not configured"
    elif not hook_enabled(project, host):
        state = "paused"
    elif not require_local_db(project).is_file():
        state = "memory missing"
    else:
        state = "enabled"
    return {"capture": state, "recall": state, "enrolled": enrolled,
            "last_saved": _read_diagnostic(project, host, "saved") if enrolled else None,
            "last_failure": _read_diagnostic(project, host, "failed") if enrolled else None}


def service(project: Path, host: str, action: str) -> dict:
    if action != "status":
        with capture_lock(project):
            if not active(project, host):
                raise ValueError("install this TRACE host integration first")
            _set_pause(project, host, action == "stop")
    return {"agent": host, **privacy_state(project, host)}


def _identifier(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= 256 and value.isascii() and value.isprintable():
        return value.strip() or None
    return None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _workspace_matches(project: Path, raw: object, *, exact: bool = False) -> bool:
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        return False
    path = Path(raw).resolve(strict=True)
    if exact:
        return path == project
    # A nested checkout or another opted-in workspace is a different memory boundary.
    if not path.is_dir() or not path.is_relative_to(project):
        return False
    while path != project:
        if any(os.path.lexists(path / name) for name in (".git", ".agent-memory")):
            return False
        path = path.parent
    return True


def dispatch(host: str, event: str, project: Path, payload: dict) -> dict:
    from memory import dialogue

    if host == "antigravity":
        roots = payload.get("workspacePaths")
        if (event != "PreInvocation" or not isinstance(roots, list) or len(roots) != 1
                or not _workspace_matches(project, roots[0], exact=True)):
            return {}
        session = _identifier(payload.get("conversationId"))
        prompt = _text(payload.get("lastUserInput"))
        turn = None
    else:
        if (host not in HOOK_HOSTS or event not in ("UserPromptSubmit", "Stop")
                or payload.get("hook_event_name") != event or not _workspace_matches(project, payload.get("cwd"))
                or payload.get("agent_id") or payload.get("agent_type")):
            return {}
        session = _identifier(payload.get("session_id"))
        prompt = _text(payload.get("prompt"))
        turn = _identifier(payload.get("turn_id" if host == "codex" else "prompt_id"))
    if session is None:
        return {}
    db = require_local_db(project)
    if not db.is_file():
        return {}
    session = f"{host}:{session}"
    stopped = event == "Stop"
    if stopped and payload.get("stop_hook_active") is True:
        return {}
    role = "assistant" if stopped else "user"
    content = _text(payload.get("last_assistant_message")) if stopped else prompt
    try:
        if dialogue.capture(db, host, session, role, content, turn):
            _diagnostic(project, host, "saved")
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 0xff
        reason = "database_busy" if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else "database_error"
        _diagnostic(project, host, "failed", reason)
        print(f"TRACE native capture unavailable ({reason})", file=sys.stderr)
        return {}
    if stopped:
        return {}
    context = dialogue.recall(db, session, prompt)
    if not context:
        return {}
    if host == "antigravity":
        return {"injectSteps": [{"ephemeralMessage": context}]}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


def _hook(host: str, project: Path, event: str) -> dict:
    if not hook_enabled(project, host):
        return {}
    raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
    if len(raw) > MAX_PAYLOAD_BYTES:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return {}
    try:
        with capture_lock(project):
            return dispatch(host, event, project, payload) if hook_enabled(project, host) else {}
    except TimeoutError:
        _diagnostic(project, host, "failed", "capture_lock")
        print("TRACE native capture unavailable (capture_lock)", file=sys.stderr)
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("hook", "mcp"))
    parser.add_argument("host", choices=(*HOOK_HOSTS, "mcp"))
    parser.add_argument("project", type=Path)
    parser.add_argument("event", nargs="?", choices=("PreInvocation", "UserPromptSubmit", "Stop"))
    args = parser.parse_args()
    project = args.project
    if args.mode == "mcp":
        from memory import mcp_server

        try:
            db = require_local_db(project)
            if not db.is_file() or (args.host != "mcp" and not host_available(project, args.host)):
                return 1
        except (OSError, ValueError, TraceUserError):
            return 1
        return mcp_server.main(
            ["--evidence", "--db", str(db), "--root", str(project)],
            check=lambda: require_local_db(project).is_file()
            and (args.host == "mcp" or host_available(project, args.host)),
        )
    try:
        output = _hook(args.host, project, args.event)
    except (OSError, ValueError, sqlite3.Error, TraceUserError) as exc:
        print(f"TRACE hook skipped ({type(exc).__name__})", file=sys.stderr)
        output = {}
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
