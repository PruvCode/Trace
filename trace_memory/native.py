"""Command endpoints for project-scoped host hooks and stdio MCP."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from memory.opencode_files import require_local_paths
from memory.opencode_service import SERVICE_DIR_NAME
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
    if event == "Stop":
        if payload.get("stop_hook_active") is not True:
            dialogue.capture(db, host, session, "assistant", _text(payload.get("last_assistant_message")), turn)
        return {}
    dialogue.capture(db, host, session, "user", prompt, turn)
    context = dialogue.recall(db, session, prompt)
    if not context:
        return {}
    if host == "antigravity":
        return {"injectSteps": [{"ephemeralMessage": context}]}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


def _hook(host: str, project: Path, event: str) -> dict:
    if os.environ.get("TRACE_DISABLE_HOOKS") == "1" or not active(project, host):
        return {}
    raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
    if len(raw) > MAX_PAYLOAD_BYTES:
        return {}
    payload = json.loads(raw)
    return dispatch(host, event, project, payload) if isinstance(payload, dict) else {}


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
            if not db.is_file() or (args.host != "mcp" and not active(project, args.host)):
                return 1
        except (OSError, ValueError, TraceUserError):
            return 1
        return mcp_server.main(
            ["--evidence", "--db", str(db), "--root", str(project)],
            check=lambda: require_local_db(project).is_file() and (args.host == "mcp" or active(project, args.host)),
        )
    try:
        output = _hook(args.host, project, args.event)
    except Exception as exc:  # A failing memory hook must let the host proceed.
        print(f"TRACE hook skipped ({type(exc).__name__})", file=sys.stderr)
        output = {}
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
