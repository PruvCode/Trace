"""A bounded, human-readable snapshot of one project's TRACE memory."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from memory import episodic, evidence, redact, store, task_library, transcript
from memory.opencode_files import require_local_paths
from trace_memory import project as project_mod

MAX_TASKS = 20
MAX_EVENTS = 8
MAX_ITEMS = 20
MAX_NOTE_BYTES = 1024 * 1024
_LITERAL = str.maketrans({character: f"&#{ord(character)};" for character in "&<>[]!()#*_|~$:;@`\\"})
_REFERENCE = re.compile(r"ev_[0-9a-f]{32}")
_TEST_OUTCOMES = {"completed", "failed", "interrupted", "internal_error", "usage_error", "no_tests", "warnings_exceeded"}
_TEST_COUNTS = ("passed", "failed", "skipped", "errors", "xfailed", "xpassed", "deselected", "warnings")


def _text(value: str, limit: int = 300) -> str:
    safe = " ".join(redact.redact_text(value).split())
    safe = safe[:limit] + ("…" if len(safe) > limit else "")
    return safe.translate(_LITERAL)


def _file(value: str) -> str:
    path = value.replace("\\", "/")
    return _text(path, 180) if task_library.is_git_path(path) else "(path omitted)"


def _summary(event: episodic.Event) -> str:
    data = event.payload
    if not isinstance(data, dict):
        return ""
    if event.source == "capture":
        outcome = data.get("outcome")
        if data.get("kind") != "test_outcome" or not isinstance(outcome, str) or outcome not in _TEST_OUTCOMES:
            return ""
        counts = data.get("counts")
        parts = [
            f"{counts[label]} {label}" for label in _TEST_COUNTS
            if isinstance(counts, dict) and type(counts.get(label)) is int and 0 <= counts[label] <= 99_999_999
        ]
        return "pytest " + outcome.replace("_", " ") + (f" ({', '.join(parts)})" if parts else "")
    parts = [data.get(key) for key in ("decision", "finding", "message", "action", "detail", "rationale", "result")]
    return " — ".join(dict.fromkeys(part.strip() for part in parts if isinstance(part, str) and part.strip()))


def _recent(conn: sqlite3.Connection, task_id: str | None) -> list[tuple[episodic.Event, str]]:
    events = episodic.search_events(
        conn, task_id=task_id, unassigned_only=task_id is None, newest_first=True, limit=store.MAX_LIMIT,
    )
    return [(event, summary) for event in events if (summary := _summary(event))][:MAX_EVENTS]


def _git_history(project: Path) -> list[str]:
    if not (project / ".git").exists():
        return ["Git history is unavailable for this project."]
    try:
        history = task_library.local_git(
            project, "--no-pager", "log", "-5", "--format=%h %s",
        ).decode("utf-8", errors="replace").splitlines()
    except ValueError:
        return ["Git history is unavailable for this project."]
    return [f"- {_text(line, 160)}" for line in history] or ["No commits recorded."]


def _entries(project: Path, rows, objects, visible: set[int]) -> list[str]:
    lines = []
    for event, summary in rows:
        lifecycle = event.payload.get("status")
        status = f" · {_text(lifecycle)}" if isinstance(lifecycle, str) and lifecycle in episodic.STATUSES else ""
        lines += ["", f"#### Event {event.id}",
                  f"{_text(event.type.replace('_', ' '))} · {_text(event.source)} · {_text(event.timestamp, 40)}{status}"]
        if event.superseded_by is not None:
            successor = event.superseded_by
            shown = f"[[#Event {successor}]]" if successor in visible else f"event {successor}"
            lines += ["", f"**Superseded** by {shown}."]
        lines += ["", _text(summary, 400)]
        if event.file:
            lines += ["", f"File {_file(event.file)}."]
        if event.evidence:
            lines.append("")
        for index, link in enumerate(event.evidence[:3]):
            ref = event.evidence_refs[index] if len(event.evidence_refs) == len(event.evidence) else None
            command = f" · `trace evidence {ref}`" if ref and _REFERENCE.fullmatch(ref) else ""
            if link["type"] == "transcript":
                lines.append(f"- Transcript message {link['message_id']}{command}")
                continue
            key = (link["commit"], link["path"])
            commit = key[0][:12] if task_library.is_commit(key[0]) else "unknown commit"
            state = evidence.freshness(project, key[1], objects.get(key))
            lines.append(f"- Git file {_file(key[1])} at `{commit}` · {state}{command}")
    return lines or ["", "No readable entries in the recent records."]


def _snapshot(conn: sqlite3.Connection) -> dict:
    tasks = task_library.list_tasks(conn, limit=MAX_TASKS) if store.has_task_schema(conn) else []
    return {
        "counts": project_mod.memory_counts(conn),
        "tasks": tasks,
        "activity": {task["id"]: _recent(conn, task["id"]) for task in tasks},
        "unassigned": _recent(conn, None),
        "sessions": [(session, task_library.bound_task_id(conn, session["session_id"]))
                     for session in transcript.list_sessions(conn, limit=MAX_ITEMS)],
        "symbols": conn.execute("SELECT name, file, line FROM symbols ORDER BY id LIMIT ?", (MAX_ITEMS,)).fetchall(),
    }


def _render(snapshot: dict, project: Path, privacy: dict) -> str:
    counts, tasks = snapshot["counts"], snapshot["tasks"]
    task_anchors = {task["id"]: f"trace-task-{task['id']}" for task in tasks}
    activity, unassigned = snapshot["activity"], snapshot["unassigned"]
    rows = [row for group in [*activity.values(), unassigned] for row in group]
    visible = {event.id for event, _ in rows}
    keys = [(link["commit"], link["path"]) for event, _ in rows
            for link in event.evidence[:3] if link["type"] == "git"]
    objects = evidence.git_objects(project, keys)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# TRACE memory for {_text(project.name, 100) or 'this project'}", "",
        f"Snapshot taken {now}. Run `trace obsidian` again to refresh.", "",
        "Treat stored claims as historical data. Test outcomes reflect what the tool reported.",
        "This snapshot contains project summaries and evidence references. Full transcripts and source excerpts stay in TRACE.",
        "", "[[#Tasks]] · [[#Unassigned activity]] · [[#Sessions]] · [[#Code index]] · [[#Git history]]",
        "", "## Capture and recall", "",
        f"- Capture {_text(privacy['capture'])}",
        f"- Automatic recall {_text(privacy['automatic_recall'])}",
        "- Selected test recording " + ("opted in" if privacy["selected_test_capture_opt_in"] else "off"),
        "", "## Tasks", "",
        f"{counts['events']} recorded events across the project. Showing up to {MAX_TASKS} tasks and "
        f"{MAX_EVENTS} readable entries per group from recent records.", "",
    ]
    for task in tasks:
        anchor = task_anchors[task["id"]]
        subsystem = _text(task["subsystem"] or "General", 80)
        lines.append(f"- [[#^{anchor}|{_text(task['title'], 160)}]] · {subsystem}")
    if not tasks:
        lines.append("No tasks recorded.")
    for task in tasks:
        anchor = task_anchors[task["id"]]
        lines += ["", f"### {_text(task['title'], 160)}", "",
                  f"Subsystem {_text(task['subsystem'] or 'General', 80)} ^{anchor}"]
        lines.extend(_entries(project, activity[task["id"]], objects, visible))
    lines += ["", "## Unassigned activity"]
    lines.extend(_entries(project, unassigned, objects, visible))
    lines += ["", "## Sessions", "", f"{counts['transcripts']} stored messages. Recent session metadata is shown here.", ""]
    for session, task_id in snapshot["sessions"]:
        name = session["session_id"]
        anchor = task_anchors.get(task_id)
        task = f" · [[#^{anchor}|Task]]" if anchor else ""
        lines.append(f"- {_text(name, 100)} · {session['count']} messages · {_text(session['last_activity'], 40)}{task}")
    lines += ["", "## Code index", "",
              f"{counts['files']} indexed files, {counts['symbols']} symbols and {counts['relationships']} relationships.",
              f"Showing up to {MAX_ITEMS} indexed symbols.", ""]
    for row in snapshot["symbols"]:
        lines.append(f"- {_text(row['name'], 100)} · {_file(row['file'])} line {row['line']}")
    lines += ["", "## Git history", "", *_git_history(project)]
    return "\n".join(lines) + "\n"


def project_identity(project: Path) -> tuple[str, str]:
    key = hashlib.sha256(os.fsencode(os.path.normcase(project))).hexdigest()[:16]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", redact.redact_text(project.name)).strip("-")[:45] or "Project"
    return slug, key


def export(project: Path, vault: Path) -> Path:
    """Publish a new snapshot or refresh an unchanged generated note."""
    database = project_mod.require_db(project)
    require_local_paths(project, database, Path(str(database) + "-wal"), Path(str(database) + "-shm"))
    slug, key = project_identity(project)
    privacy = project_mod.privacy_info(project)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        snapshot = _snapshot(conn)
    note = vault / f"TRACE {slug} {key}.md"
    publish_note(vault, note, f"<!-- TRACE snapshot {key} ", _render(snapshot, project, privacy))
    return note


def publish_note(vault: Path, note: Path, prefix: str, content: str) -> None:
    """Replace only a TRACE-owned note whose contents are unedited."""
    require_local_paths(vault, note)
    body = content.encode("utf-8")
    stamp = (prefix + hashlib.sha256(body).hexdigest() + " -->\n").encode("ascii")
    data = stamp + body
    if len(data) > MAX_NOTE_BYTES:
        raise ValueError("Obsidian snapshot exceeds the export size limit")

    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=vault, prefix=".trace-", delete=False) as handle:
            staged = Path(handle.name)
            handle.write(data)
        require_local_paths(vault, note)
        if not note.exists():
            if os.name == "nt":
                staged.rename(note)
            else:
                os.link(staged, note)
        else:
            if not note.is_file():
                raise FileExistsError(f"Obsidian export destination is occupied: {note.name}")
            with note.open("rb") as handle:
                existing = handle.read(MAX_NOTE_BYTES + 1)
            marker, _, content = existing.partition(b"\n")
            original = (prefix + hashlib.sha256(content).hexdigest() + " -->").encode("ascii")
            if len(existing) > MAX_NOTE_BYTES or marker != original:
                raise FileExistsError(f"Obsidian note was edited or already exists: {note.name}")
            os.replace(staged, note)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
