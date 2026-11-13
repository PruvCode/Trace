"""Expand stored project evidence and compare linked Git files with the current tree."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import stat
from pathlib import Path

from memory import episodic, redact, store, synthetic, task_library

MAX_SOURCE_BYTES = 64 * 1024
MAX_RENDERED_CHARS = 2000


def _blob(line: bytes) -> tuple[str, int] | None:
    fields = line.split()
    if len(fields) != 3 or fields[1] != b"blob" or not fields[2].isdigit():
        return None
    oid = fields[0].decode("ascii", errors="replace")
    if not task_library.is_commit(oid):
        return None
    return oid, int(fields[2])


def git_objects(
    project: Path | None,
    keys: list[tuple[str, str]],
) -> dict[tuple[str, str], tuple[tuple[str, int] | None, tuple[str, int] | None, bool]]:
    """Read recorded and HEAD blob metadata in one local Git call."""
    valid = list(dict.fromkeys(
        (commit, path) for commit, path in keys if task_library.is_commit(commit) and task_library.is_git_path(path)
    ))
    if not valid or project is None or not (project / ".git").exists():
        return {}
    requests = ["HEAD"]
    for commit, path in valid:
        requests.extend((commit, f"{commit}:{path}", f"HEAD:{path}"))
    try:
        lines = task_library.local_git(
            project, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
            data=("\n".join(requests) + "\n").encode("utf-8"),
        ).splitlines()
    except (ValueError, UnicodeError):
        return {}
    if len(lines) != len(requests):
        return {}
    head_ok = b" commit " in lines[0]
    result = {}
    for index, key in enumerate(valid):
        commit, recorded, head = lines[index * 3 + 1:index * 3 + 4]
        exact_commit = commit.startswith(key[0].lower().encode("ascii") + b" commit ")
        result[key] = (_blob(recorded) if exact_commit else None, _blob(head), head_ok)
    return result


def freshness(project: Path | None, path: str, objects) -> str:
    if objects is None or project is None:
        return "unknown"
    recorded, head, head_ok = objects
    if recorded is None:
        return "unknown"
    if head_ok and (head is None or head[0] != recorded[0]):
        return "stale"
    file = project / path
    try:
        if file.resolve() != file or not file.is_relative_to(project):
            return "unknown"
        info = file.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
            return "unknown"
        with file.open("rb") as handle:
            data = handle.read(MAX_SOURCE_BYTES + 1)
    except FileNotFoundError:
        return "stale"
    except (OSError, RuntimeError, ValueError):
        return "unknown"
    if len(data) > MAX_SOURCE_BYTES:
        return "unknown"

    def digest(content: bytes) -> str:
        algorithm = "sha1" if len(recorded[0]) == 40 else "sha256"
        return hashlib.new(algorithm, f"blob {len(content)}\0".encode("ascii") + content).hexdigest()

    if digest(data) == recorded[0]:
        return "current"
    # Git may check out text with CRLF on Windows while storing LF in the blob.
    if b"\0" not in data and b"\r\n" in data and digest(data.replace(b"\r\n", b"\n")) == recorded[0]:
        return "current"
    return "stale"


def expand(
    conn: sqlite3.Connection,
    project: Path | None,
    reference: str,
    *,
    task_id: str | None = None,
    offset: int = 0,
) -> str:
    """Return one framed, redacted page from an exact stored evidence reference."""
    if not isinstance(reference, str) or not re.fullmatch(r"ev_[0-9a-f]{32}", reference):
        raise ValueError("use an evidence reference returned by TRACE")
    if type(offset) is not int or not 0 <= offset <= MAX_SOURCE_BYTES:
        raise ValueError("invalid evidence offset")
    store.ensure_evidence_schema(conn)
    if task_id is not None:
        task_library.get_task(conn, task_id)
    scope = " AND e.task_id = ?" if task_id is not None else ""
    row = conn.execute(
        "SELECT l.transcript_id, l.git_commit, l.git_path, e.id AS event_id, e.task_id"
        " FROM event_evidence l JOIN events e ON e.id = l.event_id"
        f" WHERE l.reference = ?{scope}",
        (reference, task_id) if task_id is not None else (reference,),
    ).fetchone()
    if row is None:
        raise ValueError("evidence reference is unavailable in this project or task")
    superseded_by = episodic.superseded_map(conn, row["task_id"]).get(row["event_id"])
    record = {
        "reference": reference,
        "event_id": row["event_id"],
        "task_id": row["task_id"],
        "superseded_by": superseded_by,
        "lifecycle": "superseded" if superseded_by is not None else "recorded",
        "freshness": "unknown",
    }
    text = None
    if row["transcript_id"] is not None:
        record.update(type="transcript", message_id=row["transcript_id"])
        msg = conn.execute(
            "SELECT CASE WHEN length(CAST(content AS BLOB)) <= ? THEN content END AS excerpt"
            " FROM transcript_messages WHERE id = ?",
            (MAX_SOURCE_BYTES, row["transcript_id"]),
        ).fetchone()
        record["status"] = "missing" if msg is None else "too_large"
        if msg is not None:
            text = msg["excerpt"]
    else:
        commit, path = row["git_commit"], row["git_path"]
        record.update(
            type="git", commit=commit if task_library.is_commit(commit) else None,
            path=redact.redact_text(str(path or ""))[:240], status="missing",
        )
        objects = git_objects(project, [(commit, path)]).get((commit, path))
        record["freshness"] = freshness(project, path, objects)
        if objects is not None and objects[0] is not None and project is not None:
            oid, size = objects[0]
            if size <= MAX_SOURCE_BYTES:
                try:
                    data = task_library.local_git(project, "cat-file", "blob", oid)
                    if len(data) == size and b"\0" not in data:
                        text = data.decode("utf-8")
                    else:
                        record["status"] = "binary"
                except UnicodeError:
                    record["status"] = "binary"
                except ValueError:
                    pass
            else:
                record["status"] = "too_large"
    if text is None:
        return synthetic.render_evidence(record)
    safe = redact.redact_text(text)
    if offset > len(safe):
        raise ValueError("offset is past the end of the evidence")
    record.update(status="available", offset=offset)
    # Count the final framed string, including JSON escapes and reference metadata.
    low, high = 0, min(len(safe) - offset, MAX_RENDERED_CHARS)
    best = ""
    while low <= high:
        length = (low + high) // 2
        end = offset + length
        rendered = synthetic.render_evidence({
            **record, "excerpt": safe[offset:end], "next_offset": end if end < len(safe) else None,
        })
        if len(rendered) <= MAX_RENDERED_CHARS:
            best, low = rendered, length + 1
        else:
            high = length - 1
    return best
