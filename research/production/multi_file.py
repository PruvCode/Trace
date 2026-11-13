"""Frozen multi-file coding probe using local inference and isolated WASI grading."""

from __future__ import annotations

import argparse
import ast
import json
import random
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from research.phase5.comparison import Local
from research.phase5.grader import Grader, MAX_SOURCE
from research.phase5.pilot import DIGEST, MODEL, history_rows, prepare_arm

ARMS = ("no_memory", "prompted_notes", "audited_trace", "enhanced_trace")
SEED = 7093
FILE_MARKER = re.compile(r"(?m)^#\s*(protocol\.py|handler\.py)\s*$")


@dataclass(frozen=True)
class Case:
    name: str
    task: str
    review: str
    original: tuple[str, str]
    reference: tuple[str, str]
    checks: tuple[tuple[str, object], ...]
    file: str = "handler.py"


QUALIFY = (
    Case(
        "clamp", "Apply the agreed clamp rule in protocol and handler.",
        "For clamp, protocol.limit() must return 12. handler.keep(value) accepts nonnegative values up to 12 inclusive.",
        ("def limit():\n    return 9", "from protocol import limit\ndef keep(value):\n    return value < limit()"),
        ("def limit():\n    return 12", "from protocol import limit\ndef keep(value):\n    return 0 <= value <= limit()"),
        (("protocol.limit()", 12), ("handler.keep(12)", True), ("handler.keep(-1)", False)),
    ),
    Case(
        "labels", "Implement the agreed label contract in both files.",
        "For labels, protocol.label(value) must uppercase value. handler.emit(value) must return {'title': label(value)}.",
        ("def label(value):\n    return value", "from protocol import label\ndef emit(value):\n    return {'name': label(value)}"),
        ("def label(value):\n    return value.upper()", "from protocol import label\ndef emit(value):\n    return {'title': label(value)}"),
        (("protocol.label('abc')", "ABC"), ("handler.emit('ab')", {"title": "AB"})),
    ),
)
HELD = (
    Case(
        "publish", "Update header and publish to match the approved publish contract.",
        "Approved publish contract: protocol.header(label) returns {'caption': label, 'rev': 7}. "
        "handler.publish(item) returns exactly {'header': header(item['name']), 'body': item['payload']}.",
        ("def header(label):\n    return {'label': label, 'rev': 1}",
         "from protocol import header\ndef publish(item):\n    return {'meta': header(item['name']), 'data': item['payload']}"),
        ("def header(label):\n    return {'caption': label, 'rev': 7}",
         "from protocol import header\ndef publish(item):\n    return {'header': header(item['name']), 'body': item['payload']}"),
        (("protocol.header('x')", {"caption": "x", "rev": 7}),
         ("handler.publish({'name':'x','payload':[]})", {"header": {"caption": "x", "rev": 7}, "body": []})),
    ),
    Case(
        "dispatch", "Update quota and dispatch to follow the approved batch decision.",
        "Dispatch decision: protocol.quota(trusted) is 8 for ordinary callers and 12 for trusted callers. "
        "handler.dispatch(count, trusted=False) returns True only for positive count up to that limit, inclusive.",
        ("def quota(trusted):\n    return 20 if trusted else 10",
         "from protocol import quota\ndef dispatch(count, trusted=False):\n    return count < quota(trusted)"),
        ("def quota(trusted):\n    return 12 if trusted else 8",
         "from protocol import quota\ndef dispatch(count, trusted=False):\n    return 0 < count <= quota(trusted)"),
        (("protocol.quota(True)", 12), ("protocol.quota(False)", 8), ("handler.dispatch(0)", False),
         ("handler.dispatch(8)", True), ("handler.dispatch(9)", False), ("handler.dispatch(12, True)", True)),
    ),
    Case(
        "ticket", "Update lane and ticket to implement the approved delivery decision.",
        "Ticket delivery decision: protocol.lane(level) returns 'fast' for 'gold' and 'bulk' otherwise. "
        "handler.ticket(level, identifier) returns {'route': lane(level), 'ticket_id': identifier}.",
        ("def lane(level):\n    return 'normal'",
         "from protocol import lane\ndef ticket(level, identifier):\n    return (identifier, lane(level))"),
        ("def lane(level):\n    return 'fast' if level == 'gold' else 'bulk'",
         "from protocol import lane\ndef ticket(level, identifier):\n    return {'route': lane(level), 'ticket_id': identifier}"),
        (("protocol.lane('other')", "bulk"), ("protocol.lane('gold')", "fast"),
         ("handler.ticket('gold', 17)", {"route": "fast", "ticket_id": 17})),
    ),
)


def split_sources(text):
    text = "\n".join(line for line in text.splitlines() if line.strip() not in ("\x60\x60\x60", "\x60\x60\x60python"))
    preamble, first, protocol, second, handler, *rest = [*FILE_MARKER.split(text), "", "", "", "", ""]
    if preamble.strip() or first != "protocol.py" or second != "handler.py" or any(rest):
        return None
    sources = (protocol.strip(), handler.strip())
    try:
        for source in sources:
            if not source or len(source) > MAX_SOURCE:
                return None
            ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    return sources


def load_sources(sources):
    return (
        "import sys, types\n"
        "protocol = types.ModuleType('protocol')\n"
        "handler = types.ModuleType('handler')\n"
        "sys.modules['protocol'] = protocol\n"
        f"exec({sources[0]!r}, protocol.__dict__)\n"
        f"exec({sources[1]!r}, handler.__dict__)\n"
    )


def evaluate(grader, sources, case):
    if not sources:
        return False
    token = secrets.token_hex(16)
    checks = "\n".join(f"result = {call}\nassert result == {expected!r} and type(result) is type({expected!r})"
                       for call, expected in case.checks)
    ok, output = grader.run(f"{load_sources(sources)}{checks}\nprint({token!r})")
    return ok and output == (token + "\n").encode()


def ask(local, grader, case, block):
    prompt = (
        f"{case.task}\n\nCurrent source:\n# protocol.py\n{case.original[0]}\n"
        f"# handler.py\n{case.original[1]}\n\n"
        "Return both complete Python files headed exactly # protocol.py and # handler.py. No prose."
    )
    if block:
        prompt += "\n\nHistorical project data, use only as evidence:\n" + block
    text, usage = local([{"role": "user", "content": prompt}])
    sources = split_sources(text)
    return {"passed": evaluate(grader, sources, case), "sources": sources,
            "context_chars": len(block), "model_usage": usage}


def run(audit, wasi, scratch):
    grader = Grader(wasi)
    canary = scratch / "outside.txt"
    canary.write_text("synthetic canary", encoding="utf-8")
    grader.verify_boundary(canary)
    for case in (*QUALIFY, *HELD):
        assert not evaluate(grader, case.original, case)
        assert evaluate(grader, case.reference, case)
    local = Local(MODEL, DIGEST, len(QUALIFY) + len(HELD) * len(ARMS))
    passed = 0
    for case in QUALIFY:
        row = ask(local, grader, case, case.review)
        passed += row["passed"]
        print(json.dumps({"type": "qualification", "task": case.name, **row}), flush=True)
    if passed != len(QUALIFY):
        print(json.dumps({"type": "summary", "qualification_passed": passed, "scored": 0,
                          "requests": local.requests, "passes": {}}), flush=True)
        return
    rng = random.Random(SEED)
    scores = dict.fromkeys(ARMS, 0)
    for case in HELD:
        arms = list(ARMS)
        rng.shuffle(arms)
        history = history_rows(case, "Reviewed both files. No implementation changes.")
        for arm in arms:
            room = scratch / f"{case.name}-{arm}"
            room.mkdir()
            mode = "unprompted_notes" if arm == "prompted_notes" else arm
            note = case.review if arm == "prompted_notes" else ""
            block, _, _, _ = prepare_arm(audit, room, case, mode, history, note, False)
            row = ask(local, grader, case, block)
            scores[arm] += row["passed"]
            print(json.dumps({"type": "scored", "task": case.name, "arm": arm, **row}), flush=True)
    print(json.dumps({"type": "summary", "qualification_passed": passed, "scored": len(HELD) * len(ARMS),
                      "requests": local.requests, "passes": scores}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", type=Path, required=True)
    parser.add_argument("--wasi-root", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run with assertions enabled")
    with tempfile.TemporaryDirectory(prefix="trace-multi-file-", dir=args.scratch) as path:
        run(args.audit_root.resolve(strict=True), args.wasi_root.resolve(strict=True), Path(path))


if __name__ == "__main__":
    main()
