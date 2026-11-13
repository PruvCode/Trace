"""Publish TRACE's structural index as native Obsidian notes and Canvas files."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path, PurePosixPath

from memory import episodic, evidence, redact, source_files, store, structural
from memory.opencode_files import require_local_paths
from trace_memory import obsidian, project as project_mod

MAX_FILES = 10_000
MAX_LINKS = 200_000
MAX_NOTE_ITEMS = 100
MAX_CANVAS_BRANCHES = 24
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
GENERATION = "__TRACE_GENERATION__"


def _note(path: str, folder: bool) -> str:
    label = PurePosixPath(path).name if path else "Repository"
    slug = re.sub(r"[^A-Za-z0-9-]", "-", redact.redact_text(label)).strip("-")[:27] or "Path"
    digest = hashlib.sha256(path.encode()).hexdigest()[:16]
    return f"{'Folders' if folder else 'Files'}/{slug}-{digest}"


def _link(target: str, label: str) -> str:
    return f"[[{target}|{obsidian._text(label, 180)}]]"


def _local_link(project: Path, path: str) -> str:
    filename = project / path
    if redact.redact_text(filename.as_posix()) != filename.as_posix():
        return ""
    return f"[Open local file](<{filename.as_uri()}>)"


def _canvas(title: str, home: str, left: list, right: list) -> str:
    nodes = [{"id": "root", "type": "text", "x": -160, "y": -90, "width": 320, "height": 180,
              "color": "5", "text": f"# {title}\n\n{home}"}]
    edges = []
    for side, branches in (("left", left), ("right", right)):
        x = -555 if side == "left" else 290
        opposite = "right" if side == "left" else "left"
        for index, (card, inbound, outbound) in enumerate(branches):
            node_id = f"{side}-{index}"
            nodes.append({"id": node_id, "type": "text", "x": x,
                          "y": index * 105 - (len(branches) - 1) * 105 // 2 - 43,
                          "width": 265, "height": 86, "color": "6" if side == "left" else "4", "text": card})
            if inbound:
                edges.append({"id": f"{node_id}-in", "fromNode": node_id, "fromSide": opposite,
                              "toNode": "root", "toSide": side, "label": inbound})
            if outbound:
                edge = {"id": f"{node_id}-out", "fromNode": "root", "fromSide": side,
                        "toNode": node_id, "toSide": opposite}
                if outbound != "contains":
                    edge["label"] = outbound
                edges.append(edge)
    return json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False, indent=2) + "\n"


def _folder_contents(paths):
    children, files = defaultdict(set), defaultdict(list)
    for path in paths:
        parent = PurePosixPath(path).parent
        files[parent.as_posix() if parent.parts else ""].append(path)
        while parent.parts:
            ancestor = parent.parent
            children[ancestor.as_posix() if ancestor.parts else ""].add(parent.as_posix())
            parent = ancestor
    return children, files


def _relationship_lines(rows, namespace, *, inbound):
    result = []
    for source, target, kind, line, symbol, definition_line in rows[:MAX_NOTE_ITEMS]:
        peer = source if inbound else target
        origin = _link(f"{namespace}/{_note(source, False)}", PurePosixPath(source).name)
        destination = _link(f"{namespace}/{_note(target, False)}", PurePosixPath(target).name)
        where = f"{origin} line {line}" if inbound else f"Line {line} → {destination}"
        binding = f" · {obsidian._text(symbol, 100)} at line {definition_line}" if symbol else ""
        result.append(f"- {where} · {kind}{binding} · {obsidian._file(peer)}")
    if len(rows) > MAX_NOTE_ITEMS:
        result.append(f"- Showing {MAX_NOTE_ITEMS} of {len(rows)} references.")
    return result


def _render(project, paths, indexed, symbols, links, history, task_titles, focus, namespace, entry, fingerprint):
    children, files = _folder_contents(paths)
    outgoing, incoming = defaultdict(list), defaultdict(list)
    for row in links:
        outgoing[row[0]].append(row)
        incoming[row[1]].append(row)
    by_file = defaultdict(list)
    for name, kind, path, line in symbols:
        by_file[path].append((name, kind, line))
    keys = [(link["commit"], link["path"]) for events in history.values() for event in events
            for link in event.evidence[:3] if link["type"] == "git"]
    objects = evidence.git_objects(project, keys)

    def folder_link(path):
        return _link(f"{namespace}/{_note(path, True)}", PurePosixPath(path).name + "/" if path else "Repository root")

    output = {}
    for directory in sorted(set(children) | set(files) | {""}):
        label = PurePosixPath(directory).name if directory else "Repository root"
        lines = [f"# {obsidian._text(label)}", "", obsidian._file(directory) if directory else "Project files", "",
                 _link(entry, "TRACE code map")]
        if directory:
            parent = PurePosixPath(directory).parent
            lines += ["", folder_link(parent.as_posix() if parent.parts else "")]
        if children[directory]:
            lines += ["", "## Folders", "", *[f"- {folder_link(child)}" for child in sorted(children[directory])]]
        if files[directory]:
            lines += ["", "## Files", ""]
        for path in files[directory]:
            name = PurePosixPath(path).name
            local = _local_link(project, path)
            description = _link(f"{namespace}/{_note(path, False)}", name) if path in indexed else obsidian._text(name)
            lines.append(f"- {description}" + (f" · {local}" if local else ""))
        output[_note(directory, True) + ".md"] = "\n".join(lines) + "\n"

    for path in sorted(indexed):
        name = PurePosixPath(path).name
        parent = PurePosixPath(path).parent
        lines = [f"# {obsidian._text(name)}", "", obsidian._file(path), "",
                 folder_link(parent.as_posix() if parent.parts else ""), _local_link(project, path)]
        items = by_file[path]
        lines += ["", "## Symbols", ""]
        lines.extend(f"- {obsidian._text(symbol, 100)} · {kind} · line {line}"
                     for symbol, kind, line in items[:MAX_NOTE_ITEMS])
        if not items:
            lines.append("No named definitions were detected.")
        elif len(items) > MAX_NOTE_ITEMS:
            lines.append(f"Showing {MAX_NOTE_ITEMS} of {len(items)} definitions.")
        for title, rows, inbound in (
            ("Referenced by", incoming[path], True), ("References to other files", outgoing[path], False),
        ):
            lines += ["", f"## {title}", ""]
            lines.extend(_relationship_lines(rows, namespace, inbound=inbound) or ["No resolved static links."])
        records = [(event, summary) for event in history.get(path, []) if (summary := obsidian._summary(event))]
        if records:
            lines += ["", "## TRACE task history", "", "Records are linked by an explicit file or Git evidence path."]
            visible = {event.id for event, _ in records}
            for event, summary in records:
                lines.extend(obsidian._entries(project, [(event, summary)], objects, visible))
                task = task_titles.get(event.task_id)
                if task:
                    lines.append(f"- Task {obsidian._text(task, 160)}")
                if not any(link["type"] == "git" and link["path"] == path for link in event.evidence):
                    lines.append("- File revision unknown. Review the recorded evidence before reusing this result.")
        output[_note(path, False) + ".md"] = "\n".join(lines) + "\n"

    folders = sorted(children[""])
    overview = []
    for directory in folders[:MAX_CANVAS_BRANCHES]:
        preview = " · ".join(folder_link(child) for child in sorted(children[directory])[:2])
        overview.append((f"## {folder_link(directory)}" + (f"\n\n{preview}" if preview else ""), "", "contains"))
    output["Folder Tree.canvas"] = _canvas(
        f"{obsidian._text(project.name, 55)} codebase",
        f"{folder_link('')} · {_link(entry, 'Map home')}\n\n{len(paths):,} visible files",
        overview[1::2], overview[::2],
    )

    branches = [[], []]
    if focus:
        adjacent = defaultdict(lambda: [set(), set()])
        for row in incoming[focus]:
            adjacent[row[0]][0].add(row[2])
        for row in outgoing[focus]:
            adjacent[row[1]][1].add(row[2])
        for path, (before, after) in sorted(adjacent.items(), key=lambda item: ("calls" not in (item[1][0] | item[1][1]), item[0])):
            side = branches[0 if before else 1]
            if len(side) >= MAX_CANVAS_BRANCHES // 2:
                continue
            label = _link(f"{namespace}/{_note(path, False)}", PurePosixPath(path).name)
            side.append((f"## {label}\n\n{obsidian._file(path)}", ", ".join(sorted(before)), ", ".join(sorted(after))))
        title = obsidian._text(PurePosixPath(focus).name, 55)
        home = (_link(f"{namespace}/{_note(focus, False)}", "Open source note")
                + f"\n\n{len(adjacent):,} neighboring files. The source note lists reference lines.")
    else:
        title = "Code relationships"
        home = "No supported code files were found."
    output["Relationships.canvas"] = _canvas(title, home, *branches)
    output["Index metadata.json"] = json.dumps(
        {"working_index": fingerprint, "files": len(paths), "indexed": len(indexed), "links": len(links),
         "focus": redact.redact_text(focus) if focus else None},
        ensure_ascii=True,
    )
    return output


def _same_canvas(existing: bytes, expected: bytes) -> bool:
    """Obsidian can reserialize a Canvas and reorder selected cards."""
    try:
        actual, generated = json.loads(existing), json.loads(expected)
        for canvas in (actual, generated):
            for collection in ("nodes", "edges"):
                canvas[collection] = sorted(canvas[collection], key=lambda item: item["id"])
        return actual == generated
    except (ValueError, KeyError, TypeError, RecursionError):
        return False


def _publish_bundle(vault: Path, base: str, output: dict[str, str], *, max_file_bytes=obsidian.MAX_NOTE_BYTES) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(output.items()):
        digest.update(json.dumps([name, content], ensure_ascii=True).encode("ascii"))
    key = digest.hexdigest()[:16]
    parent = vault / base
    target = parent / key
    require_local_paths(vault, parent, target)
    parent.mkdir(parents=True, exist_ok=True)
    data = {
        name: content.replace(f"[[{base}/{GENERATION}/", f"[[{base}/{key}/").encode("utf-8")
        for name, content in output.items()
    }
    if sum(map(len, data.values())) > MAX_BUNDLE_BYTES or any(len(body) > max_file_bytes for body in data.values()):
        raise ValueError("TRACE bundle exceeds the export size limit")
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=parent, prefix=".trace-map-") as temp:
            stage = Path(temp) / "bundle"
            stage.mkdir()
            for name, body in data.items():
                file = stage / name
                file.parent.mkdir(exist_ok=True)
                file.write_bytes(body)
            require_local_paths(vault, parent, target)
            try:
                stage.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
    require_local_paths(vault, target)
    for name, body in data.items():
        file = target / name
        require_local_paths(target, file)
        if file.is_file() and file.stat().st_size <= max_file_bytes:
            existing = file.read_bytes()
            if existing == body or (file.suffix == ".canvas" and _same_canvas(existing, body)):
                continue
        raise FileExistsError(f"generated map snapshot was edited or is incomplete: {target.name}/{name}")
    return f"{base}/{key}"


def export(project: Path, vault: Path, focus: str | None) -> tuple[Path, Path, Path]:
    if vault.is_relative_to(project):
        raise ValueError("choose an Obsidian vault outside the indexed project")
    if focus is not None:
        focus = focus.replace("\\", "/")
    database = project_mod.require_db(project)
    files = source_files.inventory(project)
    if len(files) > MAX_FILES:
        raise ValueError(f"TRACE code maps support up to {MAX_FILES:,} visible project files")
    structural.ensure_structural_memory(project)
    fingerprint = structural._fingerprint(files)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        stored = store.get_structural_fingerprint(conn)
        if not stored or stored["file_mtimes"] != fingerprint:
            raise ValueError("source files changed during export; rerun TRACE")
        indexed = {row[0] for row in conn.execute("SELECT file FROM indexed_code_files")}
        if focus is not None and focus not in indexed:
            raise ValueError("--focus must name an indexed project-relative source file")
        links = conn.execute(
            "SELECT * FROM code_links ORDER BY source_file, target_file, source_line, kind, target_symbol LIMIT ?",
            (MAX_LINKS + 1,),
        ).fetchall()
        if len(links) > MAX_LINKS:
            raise ValueError(f"TRACE code maps support up to {MAX_LINKS:,} resolved references")
        symbols = conn.execute("SELECT name, kind, file, line FROM symbols ORDER BY file, line, name").fetchall()
        history = episodic.code_file_history(conn)
        task_titles = dict(conn.execute("SELECT id, title FROM tasks"))
    if focus is None and indexed:
        degree = Counter(path for row in links for path in row[:2])
        focus = min(indexed, key=lambda path: (-degree[path], path))
    slug, key = obsidian.project_identity(project)
    entry = f"TRACE {slug} {key} Code Map"
    base = f"TRACE Maps/{slug[:24]}-{key}"
    output = _render(project, files, indexed, symbols, links, history, task_titles, focus, f"{base}/{GENERATION}", entry, fingerprint)
    namespace = _publish_bundle(vault, base, output)
    tree, relationships = f"{namespace}/Folder Tree.canvas", f"{namespace}/Relationships.canvas"
    lines = [
        f"# TRACE code map · {obsidian._text(project.name)}", "",
        _link(tree, "Open visual folder tree"), "",
        _link(f"{namespace}/{_note('', True)}", "Browse all folders and files"), "",
        _link(relationships, "Open the relationship map"), "",
        f"{len(files):,} visible files · {len(indexed):,} indexed source files · {len(links):,} resolved references.", "",
        "Each code note links to its source, callers, imports and explicitly attached TRACE records when available.",
        "The relationship Canvas shows a bounded view around one file. Use a file note or its Obsidian local graph to explore further.",
        "Run TRACE Obsidian export again to refresh the working-tree snapshot. Earlier generated snapshots stay in the vault.",
    ]
    note = vault / f"{entry}.md"
    obsidian.publish_note(vault, note, f"<!-- TRACE code map {key} ", "\n".join(lines) + "\n")
    return note, vault / tree, vault / relationships
