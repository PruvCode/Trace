"""Resolve supported static references to files in TRACE's own source index."""

from __future__ import annotations

import ast
import json
import posixpath
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from memory import source_files
from memory.graph_model import Fragment, Observation
from memory.web_structure import WEB_SUFFIXES

# Source file, target file, relationship, source line, target symbol and definition line.
Link = tuple[str, str, str, int, str, int]


class Resolution(NamedTuple):
    file: str
    observation: Observation
    target_file: str | None
    target_name: str = ""
    target_line: int = 0


def _file(stem: str, files: set[str], suffixes: tuple[str, ...], index_name: str) -> str | None:
    stem = posixpath.normpath(stem)
    if stem in files:
        return stem
    matches = [stem + suffix for suffix in suffixes if stem + suffix in files]
    if not matches:
        matches = [f"{stem}/{index_name}{suffix}" for suffix in suffixes if f"{stem}/{index_name}{suffix}" in files]
    return matches[0] if len(matches) == 1 else None


def _entry(value) -> str | None:
    if isinstance(value, str) and value.startswith("./"):
        return value
    if isinstance(value, dict):
        values = {_entry(value[key]) for key in ("import", "default") if key in value}
        if len(values) == 1:
            return values.pop()
    return None


def read_packages(root: Path, manifests: list[str]) -> dict[str, dict]:
    packages = {}
    for path in manifests:
        try:
            manifest = json.loads(source_files.read_source(root, path) or b"{}")
        except (ValueError, UnicodeError, RecursionError):
            continue
        if not isinstance(manifest, dict) or not isinstance(manifest.get("name"), str):
            continue
        package = manifest["name"]
        if not package or package.startswith(".") or "\\" in package or "*" in package or not package.isprintable():
            continue
        packages[path] = manifest
    return packages


def _package_exports(packages: dict[str, dict]) -> dict[str, str]:
    exported = defaultdict(list)
    for path, manifest in packages.items():
        package = manifest["name"]
        entries = manifest.get("exports")
        if isinstance(entries, dict) and any(key.startswith(".") for key in entries):
            candidates = entries.items()
        else:
            candidates = [(".", entries if entries is not None else manifest.get("module", manifest.get("main")))]
        for key, value in candidates:
            entry = _entry(value)
            if entry and (key == "." or key.startswith("./")) and "*" not in key + entry:
                target = posixpath.normpath(posixpath.join(posixpath.dirname(path), entry))
                if PurePosixPath(target).is_relative_to(PurePosixPath(path).parent) and ".." not in target.split("/"):
                    exported[package if key == "." else package + key[1:]].append(target)
    return {name: targets[0] for name, targets in exported.items() if len(targets) == 1}


def _web_module(path: str, module: str, files: set[str], packages: dict[str, str]) -> str | None:
    stem = posixpath.join(posixpath.dirname(path), module) if module.startswith(("./", "../")) else packages.get(module)
    if not stem:
        return None
    stem = posixpath.normpath(stem)
    target = _file(stem, files, WEB_SUFFIXES, "index")
    extensions = {".js": (".ts", ".tsx"), ".mjs": (".mts",), ".cjs": (".cts",)}
    ext = PurePosixPath(stem).suffix
    if target is None and ext in extensions:
        present = [stem[:-len(ext)] + suffix for suffix in extensions[ext] if stem[:-len(ext)] + suffix in files]
        target = present[0] if len(present) == 1 else None
    return target


def _python_module(path: str, module: str, level: int, files: set[str]) -> str | None:
    parts = list(PurePosixPath(path).parts[:-1])
    if level > len(parts):
        return None
    if level > 1:
        parts = parts[:1 - level]
    if level == 0:
        parts = []
    if module:
        parts.extend(module.split("."))
    return _file("/".join(parts), files, (".py",), "__init__")


_PY_DEFINITION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def python_facts(tree: ast.Module) -> tuple[dict[str, list[tuple[str, int]]], list[Observation]]:
    definitions = defaultdict(list)
    observations = []
    bindings = defaultdict(list)
    top_level = set(tree.body)
    for statement in ast.walk(tree):
        if isinstance(statement, ast.Import):
            for imported in statement.names:
                observations.append(Observation("imports", imported.name, statement.lineno,
                                                statement.col_offset + 1, imported.name))
                bindings[imported.asname or imported.name.split(".")[0]].append(None)
        elif isinstance(statement, ast.ImportFrom):
            module = statement.module or ""
            observations.append(Observation("imports", "." * statement.level + module, statement.lineno,
                                            statement.col_offset + 1, module, level=statement.level))
            for imported in statement.names:
                alias = imported.asname or imported.name
                binding = (module, imported.name, statement.level)
                if imported.name != "*":
                    observations.append(Observation("references", alias, imported.lineno, imported.col_offset + 1,
                                                    *binding))
                bindings[alias].append(binding if statement in top_level and imported.name != "*" else None)
        elif isinstance(statement, _PY_DEFINITION):
            if statement in top_level:
                definitions[statement.name].append((statement.name, statement.lineno))
                bindings[statement.name].append(("", statement.name, 0))
            else:
                bindings[statement.name].append(None)
        elif isinstance(statement, ast.arg):
            bindings[statement.arg].append(None)
        elif isinstance(statement, ast.Name) and isinstance(statement.ctx, (ast.Store, ast.Del)):
            bindings[statement.id].append(None)
        elif isinstance(statement, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and statement.name:
            bindings[statement.name].append(None)
        elif isinstance(statement, ast.MatchMapping) and statement.rest:
            bindings[statement.rest].append(None)

    for statement in ast.walk(tree):
        if isinstance(statement, ast.Call):
            targets = (statement.func,)
            kind = "calls"
        elif isinstance(statement, ast.ClassDef):
            targets = statement.bases
            kind = "extends"
        else:
            continue
        for node in targets:
            if isinstance(node, ast.Name):
                name = node.id
                options = bindings.get(name, [])
                binding = options[0] if len(options) == 1 and "*" not in bindings else None
            elif isinstance(node, ast.Attribute):
                name, binding = node.attr, None
            else:
                continue
            observations.append(Observation(kind, name, node.lineno, node.col_offset + 1,
                                            *(binding or (None, None, 0))))
    return dict(definitions), observations


def resolve(fragments: dict[str, Fragment], packages: dict[str, dict]) -> list[Resolution]:
    python_files = {file for file in fragments if file.lower().endswith(".py")}
    web_files = set(fragments) - python_files
    exports = _package_exports(packages)
    result = []
    for path, fragment in fragments.items():
        targets = {}
        for observation in fragment.observations:
            module, symbol, level = observation.module, observation.symbol, observation.level
            if module is None:
                result.append(Resolution(path, observation, None))
                continue
            key = (module, level)
            if key not in targets:
                if not module and not level:
                    targets[key] = path
                elif path in python_files:
                    targets[key] = _python_module(path, module, level, python_files)
                else:
                    targets[key] = _web_module(path, module, web_files, exports)
            target = targets[key]
            if target is not None and symbol is not None:
                if not module and not level:
                    definitions = [(item.name, item.line) for item in fragments[target].definitions if item.name == symbol]
                else:
                    definitions = fragments[target].exports.get(symbol, [])
                if len(definitions) == 1:
                    result.append(Resolution(path, observation, target, *definitions[0]))
                    continue
                target = None
            result.append(Resolution(path, observation, target))
    return result


def legacy_links(resolutions: list[Resolution]) -> set[Link]:
    return {(row.file, row.target_file, row.observation.kind, row.observation.line, row.target_name, row.target_line)
            for row in resolutions if row.target_file and row.target_file != row.file
            and row.observation.kind in ("imports", "re-exports", "calls")}
