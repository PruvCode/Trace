"""Canonical structural graph and bounded queries over its SQLite snapshot."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import PurePosixPath
from typing import NamedTuple

from memory import store
from memory.code_graph import Resolution
from memory.graph_model import Fragment
from memory.web_structure import TS_SUFFIXES

ROOT = "repository"
KINDS = {
    "imports": "IMPORTS", "re-exports": "RE_EXPORTS", "calls": "CALLS",
    "references": "REFERENCES", "extends": "EXTENDS", "implements": "IMPLEMENTS",
}
CONTAINERS = {"repository", "directory", "package"}


class Node(NamedTuple):
    id: str
    parent_id: str | None
    kind: str
    name: str
    qualified_name: str
    path: str
    language: str | None = None
    line: int | None = None
    byte_column: int | None = None
    end_line: int | None = None
    end_byte_column: int | None = None


class Edge(NamedTuple):
    id: str
    source_id: str
    target_id: str | None
    kind: str
    confidence: str
    path: str
    line: int | None
    byte_column: int | None
    label: str


def _id(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()[:32]


def _language(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in TS_SUFFIXES:
        return "typescript"
    return {".py": "python", ".astro": "astro", ".gs": "apps-script"}.get(suffix, "javascript")


def _end(node: Node) -> tuple[int, int]:
    return node.end_line, node.end_byte_column


def _definition_nodes(path, fragment, module):
    stack = []
    siblings = Counter()
    for definition in sorted(fragment.definitions, key=lambda item: (item.line, item.column, -item.end_line, -item.end_column)):
        start = (definition.line, definition.column)
        end = (definition.end_line, definition.end_column)
        while stack and (start >= _end(stack[-1]) or end > _end(stack[-1])):
            stack.pop()
        parent = stack[-1] if stack else module
        kind = definition.kind
        if kind == "function" and parent.kind == "class" and module.language == "python":
            kind = "method"
        scope = (parent.id, kind, definition.name)
        ordinal = siblings[scope]
        siblings[scope] += 1
        qualified = f"{parent.qualified_name}.{definition.name}"
        node = Node(_id(*scope, ordinal), parent.id, kind, definition.name, qualified, path, module.language, *start, *end)
        stack.append(node)
        yield node


def _edge(source, target, kind, confidence, path, line, column, label):
    return Edge(_id(source, target, kind, path, line, column, label),
                source, target, kind, confidence, path, line, column, label)


def build(files: list[str], fragments: dict[str, Fragment], packages: dict[str, dict],
          resolutions: list[Resolution]) -> tuple[dict[str, Node], set[Edge], int]:
    nodes = {ROOT: Node(ROOT, None, "repository", "Repository", "", ".")}
    dirs = {str(parent) for path in files for parent in PurePosixPath(path).parents}
    package_names = {str(PurePosixPath(path).parent): manifest["name"] for path, manifest in packages.items()}
    for path in fragments:
        if PurePosixPath(path).name == "__init__.py":
            directory = PurePosixPath(path).parent
            if directory.parts:
                package_names.setdefault(str(directory), directory.name)
    folders = {".": ROOT}
    for path in sorted(dirs, key=lambda value: (len(PurePosixPath(value).parts), value)):
        if path == "." and path not in package_names:
            continue
        directory = PurePosixPath(path)
        kind = "package" if path in package_names else "directory"
        node_id = _id(kind, path)
        nodes[node_id] = Node(node_id, folders[str(directory.parent)], kind, package_names.get(path, directory.name),
                              path, path)
        folders[path] = node_id

    modules = {}
    locations = {}
    definitions = defaultdict(list)
    for path in files:
        file_path = PurePosixPath(path)
        file_id = _id("file", path)
        fragment = fragments.get(path)
        language = _language(path) if fragment is not None else None
        nodes[file_id] = Node(file_id, folders[str(file_path.parent)], "file", file_path.name, path, path, language)
        if fragment is None:
            continue
        module = Node(_id("module", path), file_id, "module", file_path.stem, path, path, language)
        modules[path] = module.id
        nodes[module.id] = module
        for node in _definition_nodes(path, fragment, module):
            nodes[node.id] = node
            definitions[path].append(node)
            locations.setdefault((path, node.name, node.line), []).append(node.id)

    starts = {path: [(node.line, node.byte_column) for node in items] for path, items in definitions.items()}
    edges = set()
    unresolved = set()
    for row in resolutions:
        observation = row.observation
        position = (observation.line, observation.column)
        index = bisect_right(starts.get(row.file, []), position) - 1
        owner = definitions[row.file][index] if index >= 0 else nodes[modules[row.file]]
        while owner.line is not None and position >= _end(owner):
            owner = nodes[owner.parent_id]
        target = None
        if row.target_file is not None:
            if row.target_name:
                matches = locations.get((row.target_file, row.target_name, row.target_line), [])
                if len(matches) == 1:
                    target = matches[0]
            else:
                target = modules[row.target_file]
        kind = KINDS[observation.kind]
        if target is None:
            unresolved.add((owner.id, kind, *position, observation.label))
        else:
            edges.add(_edge(owner.id, target, kind, "resolved", row.file, *position, observation.label))

    by_name = defaultdict(list)
    for path, manifest in packages.items():
        by_name[manifest["name"]].append(folders[str(PurePosixPath(path).parent)])
    for path, manifest in packages.items():
        source = folders[str(PurePosixPath(path).parent)]
        for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            dependencies = manifest.get(section)
            if not isinstance(dependencies, dict):
                continue
            for name in dependencies:
                targets = by_name.get(name, [])
                if len(targets) == 1 and targets[0] != source:
                    edges.add(_edge(source, targets[0], "DEPENDS_ON", "explicit", path, None, None, name))
    return nodes, edges, len(unresolved)


def publish(conn, nodes: dict[str, Node], edges: set[Edge]) -> None:
    previous_nodes = {row["id"]: Node(*row) for row in conn.execute("SELECT * FROM graph_nodes")}
    previous_edges = {Edge(*row) for row in conn.execute("SELECT * FROM graph_edges")}
    conn.executemany("DELETE FROM graph_edges WHERE id = ?", ((edge.id,) for edge in previous_edges - edges))
    changed = [node for node in nodes.values() if previous_nodes.get(node.id) != node]
    fields = Node._fields
    conn.executemany(
        f"INSERT INTO graph_nodes ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)}) "
        "ON CONFLICT(id) DO UPDATE SET " + ", ".join(f"{field}=excluded.{field}" for field in fields[1:]),
        changed,
    )
    conn.executemany("DELETE FROM graph_nodes WHERE id = ?", ((key,) for key in previous_nodes.keys() - nodes.keys()))
    conn.executemany("INSERT INTO graph_edges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", sorted(edges - previous_edges))


def _node_result(row) -> dict:
    node = dict(row)
    node["source_status"] = "indexed" if node["language"] else "inventory"
    return node


def _containment(parent: dict, child: dict) -> str:
    return "CONTAINS" if parent["kind"] in CONTAINERS or child["kind"] == "module" else "DEFINES"


def _with_unresolved(conn, focus: dict, resolved: list[dict]) -> list[dict]:
    """Project missing targets from the one file's cached facts."""
    if focus["kind"] != "module" and focus["line"] is None:
        return resolved
    (payload,) = conn.execute("SELECT payload FROM structural_fragments WHERE file=?", (focus["path"],)).fetchone()
    fragment = Fragment.decode(payload)
    children = conn.execute(
        "SELECT line, byte_column, end_line, end_byte_column FROM graph_nodes "
        "WHERE parent_id=? AND line IS NOT NULL ORDER BY line, byte_column, end_line DESC, end_byte_column DESC",
        (focus["id"],),
    ).fetchall()
    starts = [(row[0], row[1]) for row in children]
    seen = {(item["kind"], item["line"], item["byte_column"], item["label"]) for item in resolved}
    for observation in fragment.observations:
        position = (observation.line, observation.column)
        if focus["line"] is not None and not (
            (focus["line"], focus["byte_column"]) <= position < (focus["end_line"], focus["end_byte_column"])
        ):
            continue
        index = bisect_right(starts, position) - 1
        if index >= 0 and position < (children[index][2], children[index][3]):
            continue
        key = (KINDS[observation.kind], *position, observation.label)
        if key in seen:
            continue
        seen.add(key)
        edge = _edge(focus["id"], None, key[0], "unresolved", focus["path"], *position, observation.label)
        resolved.append({**edge._asdict(), "peer_name": None, "peer_path": None})
    return sorted(resolved, key=lambda edge: (edge["kind"], edge["path"], edge["line"], edge["byte_column"], edge["id"]))


def query(conn, node_id: str | None = None, name: str | None = None, limit: int = 10, offset: int = 0) -> dict:
    limit = store.clamp_limit(limit)
    if not 0 <= offset <= 1_000_000:
        raise ValueError("graph offset must be between 0 and 1000000")
    stamp = store.get_structural_fingerprint(conn)
    header = {"index_generation": stamp["file_mtimes"], "revision_kind": "working-tree", "limit": limit, "offset": offset}
    if name is not None:
        query_text = name.strip()
        rows = conn.execute(
            "SELECT n.*, f.source_hash FROM graph_nodes n LEFT JOIN structural_fragments f ON f.file=n.path"
            " WHERE n.name=? OR n.qualified_name=? OR n.path=? ORDER BY n.path, n.line, n.kind, n.id LIMIT ? OFFSET ?",
            (query_text, query_text, query_text.replace("\\", "/"), limit + 1, offset),
        ).fetchall()
        return {**header, "matches": [_node_result(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    row = conn.execute(
        "SELECT n.*, f.source_hash FROM graph_nodes n LEFT JOIN structural_fragments f ON f.file=n.path WHERE n.id=?",
        (node_id or ROOT,),
    ).fetchone()
    if row is None:
        raise ValueError("graph node was not found in this index generation")
    focus = _node_result(row)
    parent = conn.execute("SELECT * FROM graph_nodes WHERE id=?", (focus["parent_id"],)).fetchone()
    children = conn.execute(
        "SELECT * FROM graph_nodes WHERE parent_id=? ORDER BY kind, name, id LIMIT ? OFFSET ?",
        (focus["id"], limit + 1, offset),
    ).fetchall()
    result = {
        **header, "node": focus,
        "parent": {**_node_result(parent), "relationship": _containment(parent, focus)} if parent else None,
        "children": [{**_node_result(child), "relationship": _containment(focus, child)} for child in children[:limit]],
        "children_has_more": len(children) > limit,
    }
    for direction, predicate, peer in (
        ("outgoing", "source_id", "target_id"), ("incoming", "target_id", "source_id"),
    ):
        sql = (
            f"SELECT e.*, n.qualified_name AS peer_name, n.path AS peer_path FROM graph_edges e "
            f"LEFT JOIN graph_nodes n ON n.id=e.{peer} WHERE e.{predicate}=? "
            "ORDER BY e.kind, e.path, e.line, e.byte_column, e.id"
        )
        if direction == "outgoing" and (focus["kind"] == "module" or focus["line"] is not None):
            resolved = [dict(row) for row in conn.execute(sql, (focus["id"],))]
            rows = _with_unresolved(conn, focus, resolved)[offset:offset + limit + 1]
        else:
            rows = conn.execute(sql + " LIMIT ? OFFSET ?", (focus["id"], limit + 1, offset)).fetchall()
        result[direction] = [dict(item) for item in rows[:limit]]
        result[f"{direction}_has_more"] = len(rows) > limit
    return result
