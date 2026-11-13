"""Compare definition-guided edits with saved, task-matched TRACE and notes results."""

import argparse
import ast
import json
from pathlib import Path
import re
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from research.phase5.grader import Grader
from research.preview import effectiveness as harness
from research.preview.trace2_evaluation import DIGEST, QWEN
from research.preview.verification import BOUNDARY_PROBES, CASES, context, diagnostic
from research.production.multi_file import evaluate


def revise(targets, sources, model, *, case=None, grader=None):
    tokens = 0
    for target in targets:
        path, symbol = target["path"], target["symbol"]
        index = harness.FILES.index(path)
        requirements = target["decision_excerpt"]
        requirements = "\n".join(f"- {rule}" for rule in re.split(r"(?<=\.)\s+", requirements))
        prompt = (
            f"Edit {path}. Implement {symbol} according to these requirements:\n{requirements}\n\n"
            f"Current {path}:\n{sources[index]}\n\n"
            f"Other module ({harness.FILES[1-index]}):\n{sources[1-index]}\n\n"
            f"Keep the existing function signature. Review the behavior for invalid inputs. Return only the entire updated {path} in the source field."
        )
        request = {
            "model": model, "messages": [{"role": "user", "content": prompt}], "stream": False,
            "format": {"type": "object", "properties": {"source": {"type": "string"}}, "required": ["source"]},
            "keep_alive": "2m", "options": {"temperature": 0, "seed": 7093, "num_ctx": 4096, "num_predict": 384},
        }
        if case is not None:
            probes = [check for check in PUBLIC_PROBES[case.name]
                      if check[0].startswith(f"{Path(path).stem}.{symbol}(")]
            examples = "\n".join(f"{call} == {expected!r}" for call, expected in probes)
            dependencies = "\n".join(line for line in sources[index].splitlines() if line.startswith("from "))
            request["messages"][0]["content"] = (
                f"Write Python function {symbol}.\n{requirements}\n\n"
                f"Required examples:\n{examples}\n\nExisting definition:\n{sources[index]}\n"
                f"Existing imports:\n{dependencies or '(none)'}\nAvailable dependency:\n{sources[1-index]}\n\n"
                f"Output one Python def for {symbol} with the same signature. Preserve the existing imports if shown. "
                "No prose, assertions, example calls, or other definitions."
            )
            request.pop("format")
        reply = harness.request("/api/chat", request)
        tokens += reply.get("prompt_eval_count") or 0
        if case is None:
            try:
                source = json.loads(reply["message"]["content"])["source"]
            except (TypeError, ValueError, KeyError):
                continue
        else:
            source = function_only(reply["message"]["content"], symbol, sources[index])
            if source is None:
                continue
            source = dependencies + "\n" + source if dependencies else source
        candidate = list(sources)
        candidate[index] = source
        if (reply.get("done_reason") != "length" and harness.valid_sources(candidate)
                and (case is None or not any(diagnostic(grader, candidate, check) for check in probes))):
            sources = candidate
    return sources, tokens


PUBLIC_PROBES = {
    **BOUNDARY_PROBES,
    "calendar": (("protocol.day_class(5)", "weekend"), ("protocol.day_class(1)", "weekday"),
                 ("handler.reserve(3, 91)", False), ("handler.reserve(5, 45)", True)),
    "telemetry": (("protocol.band(30)", "normal"), ("protocol.band(81)", "high"),
                  ("handler.summarize([None, 20, 91])", {"low": 0, "normal": 1, "high": 1})),
}
PUBLIC_PROBES["subscription"] += (("protocol.cycle_days('expired')", None), ("protocol.cycle_days('trial')", 14))
PUBLIC_PROBES["deduplication"] += (("handler.ingest([' Q ', 'q', '  ', 'X'])", ["Q", "X"]),)


def function_only(reply, symbol, original):
    fenced = re.search(r"```(?:python)?\s*\n(.*?)```", reply, re.S)
    candidate = (fenced[1] if fenced else reply).strip()
    try:
        module = ast.parse(candidate)
        original_module = ast.parse(original)
        previous = next(node for node in original_module.body if isinstance(node, ast.FunctionDef) and node.name == symbol)
    except (SyntaxError, ValueError, RecursionError, StopIteration):
        return None
    imports = {ast.dump(node) for node in original_module.body if isinstance(node, (ast.Import, ast.ImportFrom))}
    definitions = [node for node in module.body if ast.dump(node) not in imports]
    if len(definitions) != 1 or not isinstance(definition := definitions[0], ast.FunctionDef):
        return None
    if definition.name != symbol or definition.decorator_list or ast.dump(definition.args) != ast.dump(previous.args):
        return None
    return ast.get_source_segment(candidate, definition)


def run(grader, scratch, with_probes):
    assert any(row["name"] == QWEN and row["digest"] == DIGEST for row in harness.request("/api/tags")["models"])
    canary = scratch / "canary.txt"
    canary.write_text("fixture outside WASI", encoding="utf-8")
    grader.verify_boundary(canary)
    rows = []
    for case in CASES:
        assert not evaluate(grader, case.original, case) and evaluate(grader, case.reference, case)
        _, items = context(case, "trace", scratch / case.name, with_items=True)
        decision, = [item for item in items if item.get("code_targets")]
        targets = decision["code_targets"]
        assert {target["path"] for target in targets} == set(harness.FILES) and len(targets) == 2
        start = perf_counter()
        if with_probes:
            assert all(check not in case.checks for check in PUBLIC_PROBES[case.name])
            assert not any(diagnostic(grader, case.reference, check) for check in PUBLIC_PROBES[case.name])
        sources, tokens = revise(targets, list(case.original), QWEN, case=case if with_probes else None, grader=grader)
        result = {"task": case.name, "passed": evaluate(grader, sources, case), "input_tokens": tokens,
                  "model_ms": round((perf_counter() - start) * 1000, 2), "targets": targets, "sources": sources}
        rows.append(result)
        harness.emit("scored", **result)
    harness.emit("summary", tasks=len(rows), passed=sum(row["passed"] for row in rows), requests=2 * len(rows),
                 median_input_tokens=median(row["input_tokens"] for row in rows),
                 median_model_ms=median(row["model_ms"] for row in rows))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wasi", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--public-probes", action="store_true")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run with assertions enabled")
    with TemporaryDirectory(prefix="trace-definitions-", dir=args.scratch.resolve(strict=True)) as folder:
        run(Grader(args.wasi.resolve(strict=True)), Path(folder), args.public_probes)
