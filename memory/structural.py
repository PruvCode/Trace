"""Local structural indexer for Python, JavaScript, TypeScript and Astro.

Publishes the legacy symbol index and the typed entity graph in one
transaction. Source files with matching content and parser versions reuse
cached facts. Link resolution uses the current repository inventory.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import hashlib
import json
import zlib

import tree_sitter_python
from tree_sitter import Language, Parser, Query, QueryCursor

from memory import code_graph, entity_graph, source_files, web_structure
from memory import store as store_mod
from memory.graph_model import Definition, Fragment
from memory.opencode_files import require_local_paths

_DEFS_PATTERN = """
((function_definition name: (identifier) @fname) @fnode)
((class_definition name: (identifier) @cname) @cnode)
"""
_CALLS_PATTERN = """
(call
  function: [
    (identifier) @callee
    (attribute attribute: (identifier) @callee)
  ]) @callnode
"""
_IMPORTS_PATTERN = """
(import_statement name: (dotted_name) @mod)
(import_from_statement module_name: (dotted_name) @mod)
"""


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str  # "function" | "class"
    file: str
    line: int  # 1-based first line of the definition
    end_line: int  # 1-based last line of the definition body
    signature: str
    column: int
    end_column: int


@dataclass(frozen=True)
class CallEdge:
    caller: str
    callee: str
    file: str
    line: int


@dataclass(frozen=True)
class ImportEdge:
    module: str
    file: str
    line: int


@lru_cache(maxsize=1)
def _language() -> Language:
    return Language(tree_sitter_python.language())


@lru_cache(maxsize=1)
def _parser() -> Parser:
    return Parser(_language())


@lru_cache(maxsize=1)
def _queries() -> tuple[Query, Query, Query]:
    lang = _language()
    return (
        Query(lang, _DEFS_PATTERN),
        Query(lang, _CALLS_PATTERN),
        Query(lang, _IMPORTS_PATTERN),
    )


def _text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _enclosing_function(node, source: bytes) -> str:
    current = node.parent
    while current is not None:
        if current.type == "function_definition":
            name_node = current.child_by_field_name("name")
            if name_node is not None:
                return _text(name_node, source)
            return "<anonymous>"
        current = current.parent
    return "<module>"


def extract_symbols(source: bytes, relpath: str, *, tree=None) -> list[Symbol]:
    """Definitions in one file: (name, kind, file, 1-based line, signature)."""
    tree = tree if tree is not None else _parser().parse(source)
    lines = source.decode("utf-8", errors="replace").splitlines()
    defs_query, _, _ = _queries()
    found: list[Symbol] = []
    # Each match yields exactly one definition node + its name node, paired
    # by pattern: index 0 = function, index 1 = class.
    for pattern, captures in QueryCursor(defs_query).matches(tree.root_node):
        if pattern == 0:
            kind, name_nodes, def_nodes = "function", captures.get("fname", []), captures.get("fnode", [])
        else:
            kind, name_nodes, def_nodes = "class", captures.get("cname", []), captures.get("cnode", [])
        for name_node, def_node in zip(name_nodes, def_nodes):
            line = name_node.start_point[0] + 1
            signature = lines[name_node.start_point[0]].strip()[:200] if lines else ""
            found.append(
                Symbol(
                    name=_text(name_node, source),
                    kind=kind,
                    file=relpath,
                    line=line,
                    end_line=def_node.end_point[0] + 1,
                    signature=signature,
                    column=name_node.start_point[1] + 1,
                    end_column=def_node.end_point[1] + 1,
                )
            )
    return found


def extract_calls(source: bytes, relpath: str, *, tree=None) -> list[CallEdge]:
    """Call edges in one file: (caller fn or <module>, callee, file, line)."""
    tree = tree if tree is not None else _parser().parse(source)
    _, calls_query, _ = _queries()
    found: list[CallEdge] = []
    for _pattern, captures in QueryCursor(calls_query).matches(tree.root_node):
        call_nodes = captures.get("callnode", [])
        callee_nodes = captures.get("callee", [])
        for call_node, callee_node in zip(call_nodes, callee_nodes):
            found.append(
                CallEdge(
                    caller=_enclosing_function(call_node, source),
                    callee=_text(callee_node, source),
                    file=relpath,
                    line=callee_node.start_point[0] + 1,
                )
            )
    return found


def extract_imports(source: bytes, relpath: str, *, tree=None) -> list[ImportEdge]:
    """Module imports in one file (best-effort, deterministic)."""
    tree = tree if tree is not None else _parser().parse(source)
    _, _, imports_query = _queries()
    found: list[ImportEdge] = []
    for _pattern, captures in QueryCursor(imports_query).matches(tree.root_node):
        for mod_node in captures.get("mod", []):
            found.append(
                ImportEdge(
                    module=_text(mod_node, source),
                    file=relpath,
                    line=mod_node.start_point[0] + 1,
                )
            )
    return found


INDEX_VERSION = "trace-entity-graph-2"
PARSER_VERSION = 2
CODE_SUFFIXES = (".py", *web_structure.WEB_SUFFIXES)


def _fingerprint(files) -> str:
    inputs = [
        (name, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
        if Path(name).suffix.lower() in CODE_SUFFIXES or Path(name).name == "package.json" else (name,)
        for name, info in files.items()
    ]
    return hashlib.sha256(json.dumps([INDEX_VERSION, inputs], ensure_ascii=True).encode("ascii")).hexdigest()


def _parse(path: str, source: bytes, suffix: str) -> Fragment:
    if suffix != ".py":
        return web_structure.extract(source, suffix)
    tree = _parser().parse(source)
    symbols = extract_symbols(source, path, tree=tree)
    calls = extract_calls(source, path, tree=tree)
    imports = extract_imports(source, path, tree=tree)
    try:
        exports, observations = code_graph.python_facts(ast.parse(source))
    except (SyntaxError, ValueError, RecursionError):
        exports, observations = {}, []
    return Fragment(
        [Definition(s.name, s.kind, s.line, s.column, s.end_line, s.end_column) for s in symbols],
        [(s.name, s.kind, s.line, s.signature) for s in symbols],
        [(call.caller, "calls", call.callee, call.line) for call in calls]
        + [(path, "imports", edge.module, edge.line) for edge in imports],
        exports, observations,
    )


def _publish_legacy(conn, fragments, hashes, reparsed, links):
    versions = {row[0]: (row[1], row[2]) for row in conn.execute(
        "SELECT file, parser_version, source_hash FROM structural_fragments"
    )}
    existing = {row[0] for row in conn.execute(
        "SELECT file FROM indexed_code_files UNION SELECT file FROM symbols UNION SELECT file FROM relationships"
    )}
    updated = {path for path, digest in hashes.items() if versions.get(path) != (PARSER_VERSION, digest)
               or path not in existing or path in reparsed}
    affected = [(path,) for path in sorted(updated | (existing - fragments.keys()))]
    if affected:
        conn.executemany("DELETE FROM symbols WHERE file=?", affected)
        conn.executemany("DELETE FROM relationships WHERE file=?", affected)
        conn.executemany("INSERT INTO symbols(name,kind,file,line,signature) VALUES(?,?,?,?,?)", (
            (name, kind, path, line, signature) for path in sorted(updated)
            for name, kind, line, signature in fragments[path].symbols
        ))
        conn.executemany("INSERT INTO relationships(src,rel,dst,file,line) VALUES(?,?,?,?,?)", (
            (source or path, kind, target, path, line) for path in sorted(updated)
            for source, kind, target, line in fragments[path].relationships
        ))
        store_mod.rebuild_fts(conn)
    previous = {tuple(row) for row in conn.execute("SELECT * FROM code_links")}
    conn.executemany(
        "DELETE FROM code_links WHERE (source_file,target_file,kind,source_line,target_symbol,target_line)=(?,?,?,?,?,?)",
        sorted(previous - links),
    )
    indexed = {row[0] for row in conn.execute("SELECT file FROM indexed_code_files")}
    conn.executemany("DELETE FROM indexed_code_files WHERE file=?", ((path,) for path in indexed - fragments.keys()))
    conn.executemany("INSERT INTO indexed_code_files(file) VALUES (?)", ((path,) for path in fragments.keys() - indexed))
    conn.executemany("INSERT INTO code_links VALUES (?,?,?,?,?,?)", sorted(links - previous))
    conn.executemany("DELETE FROM structural_fragments WHERE file=?", ((path,) for path in versions.keys() - fragments.keys()))
    conn.executemany("""
        INSERT INTO structural_fragments VALUES (?,?,?,?) ON CONFLICT(file) DO UPDATE SET
        parser_version=excluded.parser_version, source_hash=excluded.source_hash, payload=excluded.payload
    """, ((path, PARSER_VERSION, hashes[path], fragments[path].encode()) for path in sorted(updated)))


def index_workspace(root: Path, db_path: Path) -> dict:
    """Publish one complete structural snapshot without changing episodic rows."""
    root = root.resolve()
    db_path = db_path.absolute()
    require_local_paths(root if db_path.is_relative_to(root) else db_path.parent.resolve(),
                        db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm"))
    files = source_files.inventory(root)
    fingerprint = _fingerprint(files)
    conn = store_mod.connect(db_path)
    try:
        store_mod.init_schema(conn)
        cache = {row["file"]: (row["source_hash"], row["payload"]) for row in conn.execute(
            "SELECT file, source_hash, payload FROM structural_fragments WHERE parser_version=?", (PARSER_VERSION,)
        )}
        fragments, hashes, reparsed = {}, {}, set()
        for name in files:
            suffix = Path(name).suffix.lower()
            if suffix not in CODE_SUFFIXES:
                continue
            source = source_files.read_source(root, name)
            if source is None:
                continue
            digest = hashes[name] = hashlib.sha256(source).hexdigest()
            old_hash, data = cache.get(name, (None, None))
            if old_hash == digest:
                try:
                    fragments[name] = Fragment.decode(data)
                    continue
                except (ValueError, TypeError, KeyError, IndexError, zlib.error):
                    pass
            fragments[name] = _parse(name, source, suffix)
            reparsed.add(name)
        packages = code_graph.read_packages(root, [path for path in files if Path(path).name == "package.json"])
        resolutions = code_graph.resolve(fragments, packages)
        links = code_graph.legacy_links(resolutions)
        nodes, edges, unresolved = entity_graph.build(list(files), fragments, packages, resolutions)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if _fingerprint(source_files.inventory(root)) != fingerprint:
                raise ValueError("source files changed during indexing; rerun TRACE")
            _publish_legacy(conn, fragments, hashes, reparsed, links)
            entity_graph.publish(conn, nodes, edges)
            store_mod.set_structural_fingerprint(conn, None, fingerprint)
        return {"files": len(fragments), "symbols": sum(len(item.symbols) for item in fragments.values()),
                "relationships": sum(len(item.relationships) for item in fragments.values()),
                "resolved_links": len(links), "parsed_files": len(reparsed), "cached_files": len(fragments) - len(reparsed),
                "graph_nodes": len(nodes), "graph_edges": len(edges), "graph_unresolved": unresolved, "db": str(db_path)}
    finally:
        conn.close()


def get_current_fingerprint(project_root: Path) -> dict:
    """Fingerprint working files so edits do not need a Git commit."""
    return {"git_tree_hash": None,
            "file_mtimes": _fingerprint(source_files.inventory(project_root.resolve()))}


def is_structural_fresh(project_root: Path) -> bool:
    """Check if structural memory is up to date."""
    project_root = project_root.resolve()
    db = project_root / ".agent-memory" / "memory.db"
    require_local_paths(project_root, db, Path(str(db) + "-wal"), Path(str(db) + "-shm"))
    if not db.exists():
        return False
    conn = store_mod.connect(db)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_nodes'").fetchone():
            return False
        stored = store_mod.get_structural_fingerprint(conn)
        return bool(stored and get_current_fingerprint(project_root)["file_mtimes"] == stored["file_mtimes"])
    finally:
        conn.close()


def ensure_structural_memory(project_root: Path) -> dict:
    """Ensure structural memory exists and is fresh. Auto-create or refresh as needed."""
    project_root = project_root.resolve()
    db = project_root / ".agent-memory" / "memory.db"

    if not db.exists():
        # Fresh project - create and index
        return index_workspace(project_root, db)

    # Check if fresh
    if is_structural_fresh(project_root):
        return {"status": "fresh", "project": str(project_root)}

    # Stale - reindex
    stats = index_workspace(project_root, db)
    stats["status"] = "refreshed"
    return stats
