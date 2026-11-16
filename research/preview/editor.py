"""Shared editor mechanics for the maintained-notes coding trial.

The v1 fenced editor rejected any reply that carried module-level statements
beside the requested function. Replay showed the model legitimately re-emits
the supporting definitions it was shown (an import, a constant, an exception
class), so a mechanically correct proposal died at extraction. The v2 editor
adds a documented fallback and an optional mechanical retry. Both are
arm-independent: every arm goes through the same extraction and the same retry
policy, and neither ever sees the graded behavior checks.
"""

from __future__ import annotations

import ast
import json
import re

from memory import synthetic

SYMBOL = "{symbol}"


def _fenced(reply: str) -> str | None:
    fenced = re.search(r"```(?:python)?\s*\n(.*?)```", reply, re.S)
    if fenced is None:
        return None
    return fenced[1].strip()


def extract_function(reply: str, symbol: str, original: str) -> tuple[str | None, str | None]:
    """Extract the replacement function, returning (candidate, defect).

    The strict path is the v1 contract: the reply must yield exactly one
    non-import definition whose name and parsed signature match the pinned
    function. When that fails only because the reply carried additional
    module-level statements, the fallback extracts the single function that
    matches name and signature. Anything else is reported as a mechanical
    defect for the optional retry; the graded behavior is never consulted.
    """
    fenced = _fenced(reply)
    if fenced is None:
        try:
            ast.parse(reply)
        except (SyntaxError, ValueError, RecursionError):
            return None, "no-block"
        fenced = reply.strip()
    try:
        module = ast.parse(fenced)
        original_module = ast.parse(original)
    except (SyntaxError, ValueError, RecursionError):
        return None, "no-block"
    try:
        previous = next(node for node in original_module.body
                        if isinstance(node, ast.FunctionDef) and node.name == symbol)
    except StopIteration:
        return None, "no-block"
    imports = {ast.dump(node) for node in original_module.body if isinstance(node, (ast.Import, ast.ImportFrom))}
    definitions = [node for node in module.body if ast.dump(node) not in imports]
    strict = [node for node in definitions
              if isinstance(node, ast.FunctionDef) and node.name == symbol
              and not node.decorator_list and ast.dump(node.args) == ast.dump(previous.args)]
    if len(definitions) == 1 and strict:
        return ast.get_source_segment(fenced, strict[0]), None
    if len(strict) == 1:
        return ast.get_source_segment(fenced, strict[0]), "extra-statements"
    if strict:
        return None, "signature"
    named = [node for node in definitions if isinstance(node, ast.FunctionDef) and node.name == symbol]
    if named:
        return None, "signature"
    return None, "no-function"


def retry_feedback(defect: str, symbol: str) -> str:
    """Mechanical feedback for one retry. Mentions the defect class only."""
    reasons = {
        "no-block": "Your previous reply was not a parseable Python code block.",
        "no-function": "Your previous reply did not contain a function definition.",
        "signature": (f"Your previous reply changed the signature of {SYMBOL} or renamed its parameters. "
                      f"Keep the signature exactly as given."),
        "extra-statements": ("Your previous reply contained statements outside the function definition. "
                             f"Reply with only the complete replacement for {SYMBOL}."),
    }
    message = reasons.get(defect)
    if message is None:
        raise ValueError(f"unknown defect: {defect}")
    return (message + " Respond again with the complete updated Python function in a single Python code block. "
            "Do not include imports, module-level constants, classes, or other definitions.")


def render_excerpt(items: list[dict]) -> str:
    """Render verified decisions with their evidence as a short excerpt.

    Same retrieval output as the ordinary block, minus the record framing:
    one line per verified decision with its code target and evidence refs.
    Items without evidence, uncertain items, and messages are excluded, so a
    task with no matching history renders empty.
    """
    lines: list[str] = []
    for item in items:
        if item.get("kind") != "finding" or not item.get("evidence") or item.get("uncertain"):
            continue
        role = str(item.get("role", ""))
        if not role.startswith("decision (verified)") or not (item.get("code") or item.get("code_targets")):
            continue
        target = (item.get("code_targets") or [None])[0]
        code = item.get("code") or {}
        path = (target or {}).get("path") or code.get("path") or ""
        name = (target or {}).get("symbol") or code.get("name") or ""
        refs = ",".join(str(ref.get("ref", "")) for ref in item["evidence"][:2])
        lines.append(f"[TRACE PROJECT MEMORY] Verified decision for {path} {name} "
                     f"(evidence {refs}):\n{item.get('text')}")
    return "\n\n".join(lines)


def excerpt_from_block(block: str) -> str:
    """Recover the excerpt view from an already-rendered ordinary block."""
    items: list[dict] = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                record = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(record, dict) and "text" in record:
                items.append({
                    "kind": "finding", "text": record.get("text"), "role": record.get("finding", ""),
                    "evidence": record.get("evidence") or [], "code": record.get("code"),
                    "code_targets": record.get("code_targets"),
                    "uncertain": bool(record.get("needs_revalidation")),
                })
    return render_excerpt(items)


__all__ = [
    "SYMBOL",
    "extract_function",
    "retry_feedback",
    "render_excerpt",
    "excerpt_from_block",
    "synthetic",
]
