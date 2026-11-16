"""Per-clause diagnosis of saved framing-probe candidates (no model calls).

Splits each frozen ``changed`` / ``preserved`` check at its top-level ``and``
and runs every clause separately in the same WASI grader, so a failure can be
attributed to the rule clause the model dropped. The graded verdicts are not
changed or recomputed here; this only explains them.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from secrets import token_hex
import sys

from research.phase5.grader import Grader
from research.preview import notes_trial as nt
from research.preview.notes_cases import CASES


def clauses(expression: str) -> list[str]:
    node = ast.parse(expression, mode="eval").body
    parts = node.values if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And) else [node]
    return [ast.unparse(part) for part in parts]


def run_clauses(grader, case, candidate, checks):
    marker = token_hex(12)
    probes = ", ".join(f"check(lambda: {check})" for check in checks)
    script = (f"from __future__ import annotations\n{case.prelude}\n{nt.CHECK_HELPER}\n{candidate}\n"
              f"print({marker!r}, *[{probes}])")
    ok, output = grader.run(script)
    words = output.decode("ascii", errors="replace").strip().split()
    if not ok or len(words) != len(checks) + 1 or words[0] != marker:
        return None
    return [word == "True" for word in words[1:]]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wasi", type=Path, required=True)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    grader = Grader(args.wasi, fuel=4_000_000_000)
    cases = {nt.key(case): case for case in CASES}
    with args.output.open("w", encoding="utf-8", newline="\n") as out:
        for path in args.input:
            for row in nt.read_rows(path):
                if row["type"] != "scored" or row["candidate"] is None:
                    continue
                case = cases[row["task"]]
                changed, preserved = clauses(case.changed), clauses(case.preserved)
                result = {"source": path.name, "task": row["task"], "variant": row["variant"], "run": row["run"],
                          "seed": row["seed"], "passed": row["passed"],
                          "changed": dict(zip(changed, run_clauses(grader, case, row["candidate"], changed) or [])),
                          "preserved": dict(zip(preserved, run_clauses(grader, case, row["candidate"], preserved) or []))}
                out.write(json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
