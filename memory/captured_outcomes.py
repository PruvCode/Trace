"""Opt-in, structured pytest evidence from terminal OpenCode tool records."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from memory import episodic, redact, store, task_library, transcript

_RUN = re.compile(r"(?:pytest|(?:python(?:3)?|py) -m pytest)(?: +([^\r\n]+))?")
_FLAG = re.compile(r"-(?:q{1,2}|v{1,2}|x|s)|--(?:quiet|verbose|disable-warnings|maxfail=[1-9][0-9]{0,3})")
_TARGET = re.compile(r"(?:tests?(?:/[\w.-]+)*/?|test_[\w.-]+\.py)(?:::[\w]+)*", re.ASCII)
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_COUNT = r"[0-9]{1,8} (?:passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)"
_SUMMARY = re.compile(rf"({_COUNT}(?:, {_COUNT})*) in [0-9]+(?:\.[0-9]+)?s(?: \([0-9:.]+\))?")
_NODE = re.compile(r"(?:[\w-]+/)*(?:test_[\w-]+|[\w-]+_test)\.py(?:::[A-Za-z_][\w]*){0,2}", re.ASCII)
_FAILURE = re.compile(rf"(?:FAILED|ERROR) ({_NODE.pattern})(?:\[[^\r\n]*?\])?(?: - [^\r\n]*)?", re.ASCII)
_SHORT_SUMMARY = re.compile(r"=+ short test summary info =+")
_MAX_CHECKS = 4
_EXIT = {
    0: "completed", 1: "failed", 2: "interrupted", 3: "internal_error",
    4: "usage_error", 5: "no_tests", 6: "warnings_exceeded",
}


def configure(conn: sqlite3.Connection, action: str) -> dict:
    store.ensure_capture_schema(conn)
    with conn:
        if action == "on":
            conn.execute(
                "INSERT INTO test_capture_policy VALUES (1, ?) ON CONFLICT(id) DO NOTHING",
                (time.time_ns() // 1_000_000,),
            )
        elif action == "off":
            conn.execute("DELETE FROM test_capture_policy WHERE id = 1")
    since = _enabled_since(conn)
    return {"enabled": since is not None, "enabled_since_ms": since}


def _enabled_since(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT enabled_since_ms FROM test_capture_policy WHERE id = 1").fetchone()
    return row[0] if row is not None else None


def should_wait(conn: sqlite3.Connection, created_ms: int, part: dict, v2: bool, root: Path) -> bool:
    if part.get("type") != "tool" or part.get("name" if v2 else "tool") != ("shell" if v2 else "bash"):
        return False
    state = part.get("state")
    if not isinstance(state, dict) or state.get("status") not in ("pending", "streaming", "running"):
        return False
    since = _enabled_since(conn)
    if since is None or type(created_ms) is not int or created_ms <= since:
        return False
    return state["status"] == "streaming" or _is_pytest(state.get("input"), root)


def _is_pytest(inputs: object, root: Path, *, project_root_only: bool = False) -> bool:
    if not isinstance(inputs, dict):
        return False
    command = inputs.get("command")
    if not isinstance(command, str) or len(command) > 1024:
        return False
    match = _RUN.fullmatch(command.strip(" "))
    if not match:
        return False
    for arg in (match[1] or "").split(" "):
        if arg and not (_FLAG.fullmatch(arg) or (_TARGET.fullmatch(arg) and ".." not in arg.split("::", 1)[0].split("/"))):
            return False
    for key in ("workdir", "cwd"):
        if key not in inputs or inputs[key] is None:
            continue
        path = inputs[key]
        if not isinstance(path, str) or not path.isprintable() or len(path) > 1024:
            return False
        try:
            directory = (root / path).resolve()
            if not directory.is_relative_to(root) or (project_root_only and directory != root):
                return False
        except (OSError, RuntimeError, ValueError):
            return False
    return True


def _output_tail(state: dict, v2: bool) -> list[str]:
    if v2:
        content = state.get("content")
        if not isinstance(content, list):
            return []
        text = "\n".join(
            item["text"][-4096:] for item in content[-8:]
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        )
    else:
        text = state.get("output")
    if not isinstance(text, str):
        return []
    return _ANSI.sub("", text[-8192:]).splitlines()[-96:]


def _counts(lines: list[str]) -> dict[str, int]:
    for line in reversed(lines[-16:]):
        match = _SUMMARY.fullmatch(line.strip(" =\t"))
        if match:
            result = {}
            for item in match[1].split(", "):
                count, label = item.split(" ")
                result[{"error": "errors", "warning": "warnings"}.get(label, label)] = int(count)
            return result
    return {}


def _failed_checks(lines: list[str], root: Path) -> list[str]:
    checks = []
    in_summary = False
    for line in lines:
        if _SHORT_SUMMARY.fullmatch(line.strip()):
            in_summary = True
            continue
        if not in_summary:
            continue
        if _SUMMARY.fullmatch(line.strip(" =\t")):
            break
        match = _FAILURE.fullmatch(line)
        if not match:
            continue
        node = match[1]
        if len(node) > 128 or node in checks or redact.redact_text(node) != node:
            continue
        try:
            path = (root / node.split("::", 1)[0]).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        checks.append(node)
        if len(checks) == _MAX_CHECKS:
            break
    return checks


def failure_nodes(payload: dict) -> list[str]:
    nodes = payload.get("failed_checks")
    if (payload.get("runner") != "pytest" or payload.get("outcome") not in ("failed", "interrupted")
            or not isinstance(nodes, list)):
        return []
    return [node for node in nodes[:_MAX_CHECKS] if isinstance(node, str) and len(node) <= 128
            and _NODE.fullmatch(node) and redact.redact_text(node) == node]


def summarize(part: dict, v2: bool, root: Path) -> dict | None:
    if part.get("type") != "tool" or part.get("name" if v2 else "tool") != ("shell" if v2 else "bash"):
        return None
    state = part.get("state")
    if not isinstance(state, dict) or state.get("status") not in ("completed", "error"):
        return None
    if not _is_pytest(state.get("input"), root):
        return None
    metadata = state.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    error = state.get("error")
    if (
        metadata.get("aborted") or metadata.get("timeout") or metadata.get("signal")
        or metadata.get("status") in ("running", "aborted", "cancelled")
        or (isinstance(error, dict) and error.get("type") in ("aborted", "tool.aborted"))
    ):
        return None
    lines = _output_tail(state, v2)
    counts = _counts(lines)
    exit_code = metadata.get("exit", metadata.get("exitCode"))
    if type(exit_code) is int and 0 <= exit_code <= 255:
        outcome = _EXIT.get(exit_code, "runner_error")
        basis = "exit_code"
    elif state["status"] == "completed" and counts.keys() & {"passed", "failed", "errors", "skipped", "xfailed", "xpassed"}:
        outcome = "failed" if counts.get("failed") or counts.get("errors") else "completed"
        basis, exit_code = "pytest_summary", None
    else:
        # Some error and cancellation records carry no reliable process result.
        return None
    if outcome == "completed" and (counts.get("failed") or counts.get("errors") or state["status"] == "error"):
        outcome = "inconsistent"
    summary = {"runner": "pytest", "outcome": outcome, "basis": basis, "exit_code": exit_code, "counts": counts}
    if (outcome in ("failed", "interrupted") and (counts.get("failed") or counts.get("errors"))
            and _is_pytest(state.get("input"), root, project_root_only=True)):
        # shortcut: root-relative pytest IDs only, until capture has trustworthy rootdir metadata.
        checks = _failed_checks(lines, root)
        if checks:
            summary["failed_checks"] = checks
    return summary


def record(
    conn: sqlite3.Connection,
    autocommit: bool,
    *,
    project: Path,
    repo: str,
    session_id: str,
    message_id: str,
    tool_id: str,
    created_ms: int,
    part: dict,
    v2: bool,
) -> bool:
    since = _enabled_since(conn)
    if since is None or type(created_ms) is not int or created_ms <= since:
        return False
    ids = (session_id, message_id, tool_id)
    if not all(isinstance(value, str) and _ID.fullmatch(value) and redact.redact_text(value) == value for value in ids):
        return False
    summary = summarize(part, v2, project)
    if summary is None:
        return False
    layout = "v2" if v2 else "v1"
    digest = hashlib.sha256(json.dumps((layout, *ids)).encode("ascii")).hexdigest()
    source_id = f"test-outcome:{digest}"
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT test_outcome")
    try:
        written = _persist(conn, repo, session_id, message_id, tool_id, created_ms, layout, source_id, summary)
    except BaseException:
        conn.execute("ROLLBACK TO test_outcome")
        conn.execute("RELEASE test_outcome")
        if started:
            conn.rollback()
        raise
    conn.execute("RELEASE test_outcome")
    if autocommit and written:
        conn.commit()
    elif started and not written:
        conn.rollback()
    return written


def _persist(conn, repo, session_id, message_id, tool_id, created_ms, layout, source_id, summary) -> bool:
    # Recheck consent under the same write lock as the event and its evidence.
    since = _enabled_since(conn)
    if since is None or created_ms <= since:
        return False
    if conn.execute("SELECT 1 FROM events WHERE source_id = ?", (source_id,)).fetchone() is not None:
        return False
    task_id = task_library.bound_task_id(conn, session_id)
    description = f"pytest outcome: {summary['outcome']}"
    if summary["counts"]:
        description += " (" + ", ".join(f"{count} {label}" for label, count in summary["counts"].items()) + ")"
    evidence_text = description
    if summary.get("failed_checks"):
        evidence_text += "\nPreviously failing checks\n" + "\n".join(summary["failed_checks"])
    transcript_id = transcript.record_message(
        conn, autocommit=False, source_id=source_id, session_id=session_id, role="tool", content=evidence_text,
        metadata={"part_type": "tool", "agent": "opencode", "layout": layout,
                  "upstream_message_id": message_id, "upstream_tool_id": tool_id},
    )
    episodic.record_event(
        conn, autocommit=False, source_id=source_id, type="observation", source="capture", repo=repo,
        task_id=task_id,
        payload={"message": description, "kind": "test_outcome", **summary},
        evidence=[{"type": "transcript", "message_id": transcript_id}],
    )
    return True
