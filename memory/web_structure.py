"""Tree-sitter extraction for JavaScript, TypeScript and Astro script sections."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

import tree_sitter_html
import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Parser

from memory.graph_model import Definition, Fragment, Observation

JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".gs")
TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")
WEB_SUFFIXES = JS_SUFFIXES + TS_SUFFIXES + (".astro",)
DECLARATIONS = {
    "function_declaration": "function", "generator_function_declaration": "function",
    "class_declaration": "class", "interface_declaration": "interface", "type_alias_declaration": "type",
    "enum_declaration": "enum", "variable_declarator": "variable", "method_definition": "method",
}


@dataclass
class WebFile:
    symbols: list[tuple[str, str, int]] = field(default_factory=list)
    imports: list[tuple[str, str, int]] = field(default_factory=list)
    exports: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    calls: list[tuple[str, str, int]] = field(default_factory=list)
    definitions: list[Definition] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)


@lru_cache(maxsize=4)
def _language(dialect: str) -> Language:
    grammars = {
        "html": tree_sitter_html.language, "js": tree_sitter_javascript.language,
        "ts": tree_sitter_typescript.language_typescript, "tsx": tree_sitter_typescript.language_tsx,
    }
    return Language(grammars[dialect]())


def _walk(node):
    stack = [node]
    while stack:
        item = stack.pop()
        yield item
        stack.extend(reversed(item.named_children))


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def _sections(source: bytes, suffix: str):
    if suffix != ".astro":
        dialect = "tsx" if suffix == ".tsx" else "ts" if suffix in TS_SUFFIXES else "js"
        yield source, 0, 0, dialect
        return
    frontmatter = re.match(rb"(?:\xef\xbb\xbf)?---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|$)", source, re.S | re.M)
    offset = 0
    if frontmatter:
        yield frontmatter[1], 1, 0, "ts"
        offset = source.count(b"\n", 0, frontmatter.end())
        source = source[frontmatter.end():]
    parser = Parser(_language("html"))
    pending = [(source, offset)]
    while pending:
        markup, offset = pending.pop()
        tree = parser.parse(markup)
        for node in _walk(tree.root_node):
            if node.type != "script_element":
                continue
            tag = node.named_children[0]
            attributes = {
                _text(child.named_children[0]).lower(): _text(child.named_children[-1]).strip("'\"").lower()
                for child in tag.named_children if child.type == "attribute" and child.named_child_count
            }
            raw = next((child for child in node.named_children if child.type == "raw_text"), None)
            if raw is None:
                continue
            text = markup[raw.start_byte:raw.end_byte]
            if attributes.get("type", "") in ("", "module", "text/javascript", "application/javascript"):
                # Tuple indexing avoids the Point property refcount bug in tree-sitter 0.26.0.
                row, column = raw.start_point
                yield text, offset + row, column, "ts"
            elif "set:html" in attributes:
                # shortcut: recover scripts after Astro's self-closing data tags until a native Astro grammar is bundled.
                closing = re.search(rb"(?m)^[ \t]*/>[ \t]*\r?$", text)
                if closing and b"<script" in text[closing.end():].lower():
                    start = raw.start_byte + closing.end()
                    pending.append((markup[start:node.end_byte], offset + markup.count(b"\n", 0, start)))


def _binding_names(pattern):
    if pattern is None:
        return
    if pattern.type in ("identifier", "shorthand_property_identifier_pattern"):
        yield _text(pattern)
    elif pattern.type in ("object_pattern", "array_pattern", "rest_pattern", "formal_parameters"):
        for child in pattern.named_children:
            yield from _binding_names(child)
    elif pattern.type in ("assignment_pattern", "object_assignment_pattern"):
        yield from _binding_names(pattern.child_by_field_name("left"))
    elif pattern.type == "pair_pattern":
        yield from _binding_names(pattern.child_by_field_name("value"))
    elif pattern.type in ("required_parameter", "optional_parameter"):
        yield from _binding_names(pattern.child_by_field_name("pattern"))


def _imports(statement, module: str, include_types: bool = False):
    clause = next((child for child in statement.named_children if child.type == "import_clause"), None)
    if clause is None or (not include_types and any(child.type == "type" for child in statement.children)):
        return
    for node in clause.named_children:
        if node.type == "identifier":
            yield _text(node), (module, "default"), node
        elif node.type == "namespace_import" and node.named_child_count:
            alias = node.named_children[-1]
            yield _text(alias), (module, "*"), alias
        elif node.type == "named_imports":
            for item in node.named_children:
                if item.type == "import_specifier" and (include_types or not any(child.type == "type" for child in item.children)):
                    name = item.child_by_field_name("name")
                    if name is not None and not name.is_missing:
                        alias = item.child_by_field_name("alias") or name
                        yield _text(alias), (module, _text(name)), alias


def _declared(statement):
    if statement is None:
        return
    if statement.type in ("lexical_declaration", "variable_declaration"):
        for child in statement.named_children:
            yield from _declared(child)
    elif statement.type in DECLARATIONS:
        name = statement.child_by_field_name("name")
        if name is not None and name.type in ("identifier", "type_identifier"):
            yield _text(name), name.start_point[0]


def _position(point, offset, first_column):
    row, column = point
    return row + offset + 1, column + (first_column if row == 0 else 0) + 1


def _statement_bindings(tree, result: WebFile, offset: int, first_column: int):
    imports = {}
    type_imports = {}
    top_level = {}
    pending = []
    for statement in tree.named_children:
        declaration = statement.child_by_field_name("declaration") if statement.type == "export_statement" else statement
        for name, row in _declared(declaration):
            top_level.setdefault(name, []).append(row + offset + 1)
            if statement.type == "export_statement":
                public = "default" if any(child.type == "default" for child in statement.children) else name
                result.exports.setdefault(public, []).append((name, row + offset + 1))
        if statement.type not in ("import_statement", "export_statement"):
            continue
        source = statement.child_by_field_name("source")
        if source is not None:
            value = _text(source)
            if len(value) < 2 or value[0] not in "'\"" or "\\" in value:
                continue
            module = value[1:-1]
            kind = "imports" if statement.type == "import_statement" else "re-exports"
            line, column = _position(source.start_point, offset, first_column)
            result.imports.append((kind, module, line))
            result.observations.append(Observation(kind, module, line, column, module))
            for alias, binding, _token in _imports(statement, module):
                imports.setdefault(alias, []).append(binding)
            for alias, binding, token in _imports(statement, module, include_types=True):
                type_imports.setdefault(alias, []).append(binding)
                if binding[1] != "*":
                    location = _position(token.start_point, offset, first_column)
                    result.observations.append(Observation("references", alias, *location, *binding))
        elif statement.type == "export_statement":
            clause = next((node for node in statement.named_children if node.type == "export_clause"), None)
            if clause is not None:
                for item in clause.named_children:
                    if item.type == "export_specifier":
                        name = _text(item.child_by_field_name("name"))
                        pending.append((_text(item.child_by_field_name("alias")) or name, name))
    for public, name in pending:
        result.exports.setdefault(public, []).extend((name, row) for row in top_level.get(name, []))
    return imports, type_imports, top_level


def _call_target(node, imports, shadowed):
    if node is None:
        return "", None
    if node.type == "identifier":
        name = base = _text(node)
    elif node.type == "member_expression":
        obj, prop = node.child_by_field_name("object"), node.child_by_field_name("property")
        name = _text(prop)
        base = _text(obj) if obj is not None and obj.type == "identifier" else ""
    else:
        return "", None
    bindings = imports.get(base, ())
    if base in shadowed or len(bindings) != 1:
        return name, None
    module, imported = bindings[0]
    if node.type == "member_expression":
        return name, (module, name) if imported == "*" else None
    return name, (module, imported) if imported != "*" else None


def _heritage(node, imports, local, shadowed, offset, first_column):
    if node.type not in ("extends_clause", "extends_type_clause", "implements_clause", "class_heritage"):
        return
    kind = "implements" if node.type == "implements_clause" else "extends"
    for child in node.named_children:
        target = child.child_by_field_name("name") if child.type == "generic_type" else child
        if target is None or target.type not in ("identifier", "type_identifier"):
            continue
        label = _text(target)
        bindings = imports.get(label, [])
        if len(bindings) == 1 and not shadowed[label] and bindings[0][1] != "*":
            module, symbol = bindings[0]
        elif not bindings and len(local.get(label, [])) == shadowed[label] == 1:
            module, symbol = "", label
        else:
            module, symbol = None, None
        line, column = _position(target.start_point, offset, first_column)
        yield Observation(kind, label, line, column, module, symbol)


def extract(source: bytes, suffix: str) -> Fragment:
    result = WebFile()
    for section, offset, first_column, dialect in _sections(source, suffix):
        parser = Parser(_language(dialect))
        tree = parser.parse(section)
        root = tree.root_node
        imports, type_imports, local = _statement_bindings(root, result, offset, first_column)
        shadowed: Counter[str] = Counter()
        for node in _walk(root):
            kind = DECLARATIONS.get(node.type)
            if kind:
                name = node.child_by_field_name("name")
                if name is not None and name.type in ("identifier", "type_identifier", "property_identifier"):
                    if node.type != "method_definition":
                        shadowed[_text(name)] += 1
                    if node.type == "variable_declarator":
                        value = node.child_by_field_name("value")
                        if value is not None and value.type in ("arrow_function", "function_expression"):
                            kind = "function"
                    result.symbols.append((_text(name), kind, name.start_point[0] + offset + 1))
                    result.definitions.append(Definition(_text(name), kind,
                        *_position(name.start_point, offset, first_column), *_position(node.end_point, offset, first_column)))
                elif node.type == "variable_declarator":
                    shadowed.update(_binding_names(name))
            elif node.type == "internal_module":
                name = node.child_by_field_name("name")
                if name is not None and name.type in ("identifier", "nested_identifier"):
                    result.definitions.append(Definition(_text(name), "namespace",
                        *_position(name.start_point, offset, first_column), *_position(node.end_point, offset, first_column)))
            if node.type in ("formal_parameters", "catch_clause", "arrow_function", "function_expression"):
                param = node if node.type == "formal_parameters" else node.child_by_field_name("parameter")
                shadowed.update(_binding_names(param))
                if node.type == "function_expression":
                    shadowed.update(_binding_names(node.child_by_field_name("name")))
        # shortcut: a shadow anywhere in the script suppresses that resolved name, until lexical scoping is needed.
        for node in _walk(root):
            result.observations.extend(_heritage(node, type_imports, local, shadowed, offset, first_column))
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            name, binding = _call_target(callee, imports, shadowed)
            if not name:
                continue
            caller = "<module>"
            parent = node.parent
            while parent is not None:
                if parent.type in ("function_declaration", "method_definition", "generator_function_declaration"):
                    caller = _text(parent.child_by_field_name("name")) or "<anonymous>"
                    break
                if parent.type in ("arrow_function", "function_expression"):
                    owner = parent.parent
                    caller = _text(owner.child_by_field_name("name")) if owner.type == "variable_declarator" else "<anonymous>"
                    break
                parent = parent.parent
            line, column = _position(node.start_point, offset, first_column)
            result.calls.append((caller, name, line))
            if binding is None and callee.type == "identifier" and not imports.get(name):
                if len(local.get(name, [])) == shadowed[name] == 1:
                    binding = ("", name)
            result.observations.append(Observation("calls", name, line, column, *(binding or (None, None))))
    return Fragment(
        result.definitions, [(name, kind, line, "") for name, kind, line in result.symbols],
        [("", kind, module, line) for kind, module, line in result.imports]
        + [(caller, "calls", name, line) for caller, name, line in result.calls],
        result.exports, result.observations,
    )
