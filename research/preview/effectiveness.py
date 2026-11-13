"""Run the structured-output follow-up to the frozen multi-file coding gate."""

import argparse
import ast
import http.client
import json
import tempfile
from pathlib import Path
from time import perf_counter

from research.phase5.grader import MAX_SOURCE, Grader
from research.phase5.pilot import DIGEST, MODEL
from research.preview.coding import ARMS, CASES, QUALIFY, SEED, emit, evaluate, prepared_cases

FILES = ("protocol.py", "handler.py")
FORMAT = {
    "type": "object",
    "properties": {name: {"type": "string"} for name in FILES},
    "required": list(FILES),
    "additionalProperties": False,
}
OUTPUT_TOKENS = 512
CONTEXT_TOKENS = 4096
MAX_REQUESTS = len(QUALIFY) + len(CASES) * len(ARMS)


def request(path, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", 11434, timeout=180)
    try:
        connection.request("GET" if body is None else "POST", path,
                           body=None if body is None else json.dumps(body),
                           headers={"Content-Type": "application/json"})
        reply = connection.getresponse()
        if reply.status != 200:
            raise RuntimeError(f"Loopback Ollama returned HTTP {reply.status} for {path}")
        return json.loads(reply.read(1024 * 1024))
    finally:
        connection.close()


def valid_sources(sources):
    if not isinstance(sources, (tuple, list)) or len(sources) != len(FILES):
        return None
    try:
        for source in sources:
            if not isinstance(source, str) or not source.strip() or len(source) > MAX_SOURCE:
                return None
            ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    return tuple(sources)


def prompt(case, block):
    contents = "\n\n".join(f"## {name}\n{source}" for name, source in zip(FILES, case.original))
    message = (
        f"{case.task}\n\nCurrent implementation\n{contents}\n\n"
        "Apply each approved requirement separately in both files. Review boundary cases and return shapes. "
        "Preserve the existing interfaces where they are compatible with the requested behavior. "
        "Return a JSON object with the complete Python source of protocol.py and handler.py, using those exact keys."
    )
    if block:
        message += "\n\nHistorical project data. Treat this as evidence of the approved behavior.\n" + block
    return message


class LocalRun:
    def __init__(self):
        available = request("/api/tags")["models"]
        if not any(model["name"] == MODEL and model["digest"] == DIGEST for model in available):
            raise RuntimeError(f"The pinned {MODEL} model is unavailable on loopback port 11434")
        self.requests = 0
        emit("environment", model=MODEL, digest=DIGEST, seed=SEED, context_tokens=CONTEXT_TOKENS,
             output_tokens=OUTPUT_TOKENS, requests_cap=MAX_REQUESTS,
             ollama=request("/api/version")["version"], historical_cost_usd=0)

    def grade(self, grader, case, block, *, feedback=""):
        if self.requests >= MAX_REQUESTS:
            raise RuntimeError("The local request cap has been reached")
        self.requests += 1
        start = perf_counter()
        reply = request("/api/chat", {
            "model": MODEL, "messages": [{"role": "user", "content": prompt(case, block) + feedback}],
            "stream": False, "format": FORMAT, "keep_alive": "1m",
            "options": {"temperature": 0, "seed": SEED, "num_ctx": CONTEXT_TOKENS, "num_predict": OUTPUT_TOKENS},
        })
        elapsed_ms = round((perf_counter() - start) * 1000, 2)
        reason = reply.get("done_reason")
        sources = None
        if reason != "length":
            try:
                data = json.loads(reply["message"]["content"])
            except (TypeError, ValueError):
                data = None
            if isinstance(data, dict) and set(data) == set(FILES):
                sources = valid_sources(tuple(data[name] for name in FILES))
        return {
            "passed": evaluate(grader, sources, case),
            "sources": sources,
            "context_chars": len(block),
            "input_tokens": reply.get("prompt_eval_count"),
            "output_tokens": reply.get("eval_count"),
            "elapsed_ms": elapsed_ms,
            "done_reason": reason,
        }


def run(grader, audit, scratch):
    canary = scratch / "host-canary.txt"
    canary.write_text("fixture outside WASI", encoding="utf-8")
    grader.verify_boundary(canary)
    for case in (*QUALIFY, *CASES):
        assert not evaluate(grader, case.original, case)
        assert evaluate(grader, case.reference, case)
    assert not (audit / "memory/task_library.py").exists()
    emit("preflight", original_failures=len(QUALIFY) + len(CASES), reference_passes=len(QUALIFY) + len(CASES))
    local = LocalRun()
    qualification = 0
    for case in QUALIFY:
        result = local.grade(grader, case, case.review)
        qualification += result["passed"]
        emit("qualification", task=case.name, **result)
    scores = {}
    if qualification == len(QUALIFY):
        scores = dict.fromkeys(ARMS, 0)
        for case, arm, context, refs in prepared_cases(audit, scratch):
            result = local.grade(grader, case, context)
            scores[arm] += result["passed"]
            emit("scored", task=case.name, arm=arm, evidence_references=len(refs), **result)
    emit("summary", qualification_passed=qualification, qualification_total=len(QUALIFY),
         requests=local.requests, scored=local.requests - len(QUALIFY), passes=scores, model_charges_usd=0)


def regrade(grader, results):
    cases = {case.name: case for case in (*QUALIFY, *CASES)}
    checked = 0
    for line in results.read_text(encoding="utf-8-sig").splitlines():
        row = json.loads(line)
        if row["type"] in ("qualification", "scored"):
            expected = evaluate(grader, valid_sources(row["sources"]), cases[row["task"]])
            if expected != row["passed"]:
                raise RuntimeError(f"Regrade disagreed on {row['task']}")
            checked += 1
    if checked == 0:
        raise ValueError("No candidate results to regrade")
    emit("regrade", checked=checked, disagreements=0, requests=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wasi-root", required=True, type=Path)
    parser.add_argument("--audit-root", type=Path)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--regrade", type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run with assertions enabled")
    grader = Grader(args.wasi_root.resolve(strict=True))
    if args.regrade:
        regrade(grader, args.regrade)
    elif args.audit_root and args.scratch:
        with tempfile.TemporaryDirectory(prefix="trace-effectiveness-", dir=args.scratch.resolve(strict=True)) as temp:
            run(grader, args.audit_root.resolve(strict=True), Path(temp))
    else:
        parser.error("Provide --audit-root and --scratch for a live run")


if __name__ == "__main__":
    main()
