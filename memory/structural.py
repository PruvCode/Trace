"""Tree-sitter structural indexer (Python only, Phase 2).

Extracts function/class definitions, call edges, and module imports into the
store. Deterministic: files visited in sorted order, tables cleared before
each index run.

Known limitations (documented, not worked around):
- Attribute calls (`obj.method()`) are recorded under the attribute name, so
  same-named methods on different classes conflate. MVP accepts this; the
  benchmark measures downstream task outcomes, not graph perfection.
- Dynamic calls (`getattr`, decorators-as-calls aside) and star-import
  re-exports are not resolved.
- Only `.py` files under the size cap are indexed; binaries are skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import hashlib
import json
import subprocess

import tree_sitter_python
from tree_sitter import Language, Parser, Query, QueryCursor

from memory import store as store_mod

SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        ".agent-memory",
        ".pytest_cache",
        ".hypothesis",
        "node_modules",
        ".hg",
    }
)
MAX_FILE_BYTES = 256_000

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


def extract_symbols(source: bytes, relpath: str) -> list[Symbol]:
    """Definitions in one file: (name, kind, file, 1-based line, signature)."""
    tree = _parser().parse(source)
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
                )
            )
    return found


def extract_calls(source: bytes, relpath: str) -> list[CallEdge]:
    """Call edges in one file: (caller fn or <module>, callee, file, line)."""
    tree = _parser().parse(source)
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


def extract_imports(source: bytes, relpath: str) -> list[ImportEdge]:
    """Module imports in one file (best-effort, deterministic)."""
    tree = _parser().parse(source)
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


def iter_python_files(root: Path) -> list[Path]:
    """Sorted .py files under root, minus skipped dirs/oversized/binary."""
    collected: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            with open(path, "rb") as fh:
                head = fh.read(8192)
            if b"\x00" in head:
                continue
        except OSError:
            continue
        collected.append(path)
    return collected


def index_workspace(root: Path, db_path: Path) -> dict:
    """(Re)build the structural index for root into db_path. Deterministic."""
    root = root.resolve()
    conn = store_mod.connect(db_path)
    try:
        store_mod.init_schema(conn)
        store_mod.clear(conn)
        files = symbol_count = rel_count = 0
        for path in iter_python_files(root):
            relpath = path.relative_to(root).as_posix()
            try:
                source = path.read_bytes()
            except OSError:
                continue
            files += 1
            for symbol in extract_symbols(source, relpath):
                store_mod.insert_symbol(
                    conn, symbol.name, symbol.kind, symbol.file,
                    symbol.line, symbol.signature,
                )
                symbol_count += 1
            for edge in extract_calls(source, relpath):
                store_mod.insert_relationship(
                    conn, edge.caller, "calls", edge.callee,
                    edge.file, edge.line,
                )
                rel_count += 1
            for edge in extract_imports(source, relpath):
                store_mod.insert_relationship(
                    conn, edge.file, "imports", edge.module,
                    edge.file, edge.line,
                )
                rel_count += 1
        store_mod.rebuild_fts(conn)
        conn.commit()
        # Store fingerprint after successful index
        _store_fingerprint(root, conn)
        return {
            "files": files,
            "symbols": symbol_count,
            "relationships": rel_count,
            "db": str(db_path),
        }
    finally:
        conn.close()


def _compute_git_tree_hash(project_root: Path) -> str | None:
    """Compute Git tree hash for the repository (deterministic)."""
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "HEAD", "--", "."],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        # Hash the tree output for a stable fingerprint
        return hashlib.sha256(result.stdout.encode()).hexdigest()[:32]
    except (OSError, subprocess.SubprocessError):
        return None


def _compute_file_mtimes(project_root: Path) -> str | None:
    """Compute file mtimes hash as fallback for non-Git repos."""
    try:
        files = iter_python_files(project_root)
        if not files:
            return None
        mtime_data = {}
        for path in files:
            relpath = path.relative_to(project_root).as_posix()
            try:
                mtime_data[relpath] = int(path.stat().st_mtime * 1e6)  # microseconds
            except OSError:
                continue
        if not mtime_data:
            return None
        # Create deterministic JSON and hash it
        json_str = json.dumps(mtime_data, sort_keys=True)
        return hashlib.sha256(json_str.encode()).hexdigest()[:32]
    except Exception:
        return None


def _store_fingerprint(project_root: Path, conn) -> None:
    """Store the current fingerprint after indexing."""
    git_hash = _compute_git_tree_hash(project_root)
    file_hash = _compute_file_mtimes(project_root)
    store_mod.set_structural_fingerprint(conn, git_hash, file_hash)


def get_current_fingerprint(project_root: Path) -> dict:
    """Compute the current fingerprint of the project."""
    return {
        "git_tree_hash": _compute_git_tree_hash(project_root),
        "file_mtimes": _compute_file_mtimes(project_root),
    }


def is_structural_fresh(project_root: Path) -> bool:
    """Check if structural memory is up to date."""
    db = project_root / ".agent-memory" / "memory.db"
    if not db.exists():
        return False
    conn = store_mod.connect(db)
    try:
        stored = store_mod.get_structural_fingerprint(conn)
        if not stored:
            return False
        current = get_current_fingerprint(project_root)
        # Compare git hash if available, otherwise file mtimes
        if current["git_tree_hash"] and stored["git_tree_hash"]:
            return current["git_tree_hash"] == stored["git_tree_hash"]
        if current["file_mtimes"] and stored["file_mtimes"]:
            return current["file_mtimes"] == stored["file_mtimes"]
        return False
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
