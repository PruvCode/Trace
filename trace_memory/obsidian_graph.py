"""Publish bounded, read-only views of the canonical graph for Obsidian."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from importlib.resources import files
import json
from pathlib import Path
import sqlite3
import tempfile

from memory import entity_graph, entity_history, redact, store, structural
from memory.opencode_files import require_local_paths
from trace_memory import code_map, obsidian, project as project_mod

DATA_DIR = ".trace-graphs"
PLUGIN_ID = "trace-code-graph"
MAX_NODES = 50_000
MAX_EDGES = 200_000
MAX_FILE_BYTES = 8 * 1024 * 1024
ASSETS = ("manifest.json", "main.js", "styles.css")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _node(row) -> dict:
    path = redact.redact_text(row["path"])
    return {
        "id": row["id"], "parent": row["parent_id"], "kind": row["kind"],
        "name": redact.redact_text(row["name"])[:180], "path": path,
        "line": row["line"],
        "openable": row["kind"] not in entity_graph.CONTAINERS and path == row["path"],
    }


def _snapshot(conn: sqlite3.Connection, name: str) -> tuple[dict, dict[str, str]]:
    rows = conn.execute(
        "SELECT * FROM graph_nodes ORDER BY kind, name, path, line, id LIMIT ?", (MAX_NODES + 1,),
    ).fetchall()
    if not rows or len(rows) > MAX_NODES:
        raise ValueError(f"the Obsidian view needs a graph with 1 to {MAX_NODES:,} nodes")
    nodes = {row["id"]: _node(row) for row in rows}
    children = defaultdict(list)
    for node in nodes.values():
        if node["parent"] is not None:
            children[node["parent"]].append(node["id"])
    for node in nodes.values():
        node["children"] = len(children.get(node["id"], ()))
    shards = defaultdict(dict)
    for node_id, node in nodes.items():
        shards[node_id[0]][node_id] = {
            "node": node, "children": [nodes[child] for child in children.get(node_id, ())],
        }
    incoming, outgoing = defaultdict(list), defaultdict(list)
    count = 0
    for row in conn.execute(
        "SELECT * FROM graph_edges ORDER BY kind, path, line, byte_column, id LIMIT ?", (MAX_EDGES + 1,),
    ):
        count += 1
        if count > MAX_EDGES:
            raise ValueError(f"the Obsidian view supports up to {MAX_EDGES:,} resolved relationships")
        location = redact.redact_text(row["path"])
        link = [row["kind"], row["confidence"], location, row["line"], row["byte_column"], location == row["path"]]
        source, target = nodes[row["source_id"]], nodes[row["target_id"]]
        outgoing[source["id"]].append([target["id"], *link, target["name"]])
        incoming[target["id"]].append([source["id"], *link, source["name"]])
    links = defaultdict(dict)
    for node_id in incoming.keys() | outgoing.keys():
        links[node_id[0]][node_id] = {"incoming": incoming[node_id], "outgoing": outgoing[node_id]}
    root = entity_graph.ROOT
    metadata = {
        "version": 1, "name": name, "root": root, "nodeCount": len(nodes), "edgeCount": count,
        "links": sorted(links), "indexGeneration": store.get_structural_fingerprint(conn)["file_mtimes"],
    }
    # Search summaries carry names and locations. Branch and relationship details stay in their own shards.
    catalog = [[redact.redact_text(row["qualified_name"])[:600], nodes[row["id"]]]
               for row in rows]
    output = {"catalog.json": _json(catalog)}
    output.update({f"nodes-{key}.json": _json(data) for key, data in shards.items()})
    output.update({f"links-{key}.json": _json(data) for key, data in links.items()})
    return metadata, output


def export(project: Path, vault: Path, *, include_history: bool = False) -> Path:
    if vault.is_relative_to(project):
        raise ValueError("choose an Obsidian vault outside the indexed project")
    database = project_mod.require_db(project)
    structural.ensure_structural_memory(project)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        metadata, output = _snapshot(conn, redact.redact_text(project.name)[:100])
        if include_history:
            attached, records = entity_history.export(conn, project)
            output["history.json"] = _json(redact.redact_value({"attached": attached, "records": records}))
        metadata["history"] = include_history
    slug, key = obsidian.project_identity(project)
    base = f"{DATA_DIR}/{slug}-{key}"
    generation = code_map._publish_bundle(vault, base, output, max_file_bytes=MAX_FILE_BYTES).rsplit("/", 1)[1]
    entry = vault / base / "current.json"
    obsidian.publish_note(vault, entry, f"<!-- TRACE graph {key} ", _json({**metadata, "generation": generation}) + "\n")
    return entry


def install(vault: Path) -> Path:
    """Install the packaged files while preserving the vault's plugin settings."""
    parent = vault / ".obsidian" / "plugins"
    target = parent / PLUGIN_ID
    require_local_paths(vault, parent, target)
    parent.mkdir(parents=True, exist_ok=True)
    assets = files("trace_memory").joinpath("obsidian_graph_plugin")
    content = {name: assets.joinpath(name).read_bytes() for name in ASSETS}
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=parent, prefix=".trace-plugin-") as temp:
            stage = Path(temp) / PLUGIN_ID
            stage.mkdir()
            for name, data in content.items():
                (stage / name).write_bytes(data)
            require_local_paths(vault, target)
            try:
                stage.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
    for name, data in content.items():
        path = target / name
        require_local_paths(vault, path)
        if not path.is_file() or path.stat().st_size != len(data) or path.read_bytes() != data:
            raise FileExistsError(f"existing plugin files were kept: {target.name}/{name}")
    return target
