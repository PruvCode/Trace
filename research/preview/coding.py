"""Compare four memory conditions after qualifying a pinned loopback model."""

import argparse
import http.client
import json
import random
import tempfile
from pathlib import Path
from time import perf_counter

from research.phase5.comparison import ARMS
from research.phase5.grader import Grader
from research.phase5.pilot import AUDIT, history_rows, prepare_arm
from research.production.multi_file import HELD, QUALIFY, SEED, Case, ask, evaluate

CASES = (*HELD,
    Case(
        "pagination", "Update the page size parser and response to follow the approved pagination contract.",
        "Pagination contract: page_size accepts integers or numeric strings, defaults to 25 on invalid "
        "input, and clamps to 1 through 100. response returns {'items': first size items, 'more': "
        "whether any remain} without mutating the input.",
        ("def page_size(raw):\n    return int(raw)",
         "from protocol import page_size\ndef response(items, raw):\n    return items[:page_size(raw)]"),
        ("def page_size(raw):\n    try:\n        return max(1, min(100, int(raw)))\n"
         "    except (ValueError, TypeError):\n        return 25",
         "from protocol import page_size\ndef response(items, raw):\n    size = page_size(raw)\n"
         "    return {'items': items[:size], 'more': len(items) > size}"),
        (("protocol.page_size('bad')", 25), ("protocol.page_size(None)", 25),
         ("protocol.page_size(-5)", 1), ("protocol.page_size('101')", 100),
         ("handler.response([1, 2, 3], '2')", {"items": [1, 2], "more": True}),
         ("handler.response([1, 2], '2')", {"items": [1, 2], "more": False}),
         ("handler.response([], None)", {"items": [], "more": False})),
    ),
    Case(
        "receipt", "Update currency normalization and receipt creation using the approved receipt rules.",
        "Receipt rules: currency strips surrounding spaces and uppercases the value. It permits only "
        "USD or EUR and returns None otherwise. receipt returns None for invalid currency or negative "
        "cents. Otherwise it returns {'currency': normalized, 'total_cents': cents}, including zero.",
        ("def currency(raw):\n    return raw.lower()",
         "from protocol import currency\ndef receipt(raw, cents):\n    return (currency(raw), cents)"),
        ("def currency(raw):\n    code = raw.strip().upper()\n    return code if code in ('USD', 'EUR') else None",
         "from protocol import currency\ndef receipt(raw, cents):\n    code = currency(raw)\n"
         "    if code is None or cents < 0:\n        return None\n"
         "    return {'currency': code, 'total_cents': cents}"),
        (("protocol.currency(' eur ')", "EUR"), ("protocol.currency('JPY')", None),
         ("handler.receipt('USD', 0)", {"currency": "USD", "total_cents": 0}),
         ("handler.receipt(' EUR ', 470)", {"currency": "EUR", "total_cents": 470}),
         ("handler.receipt('EUR', -1)", None), ("handler.receipt('gbp', 200)", None)),
    ),
)
MODELS = {
    "qwen2.5-coder:3b": 256,
    "qwen3:4b-instruct-2507-q4_K_M": 256,
    "qwen3:4b-thinking-2507-q4_K_M": 400,
    "qwen3.5:2b": 256,
}


def emit(kind, **record):
    print(json.dumps({"type": kind, **record}), flush=True)


def local_request(path, data=None):
    connection = http.client.HTTPConnection("127.0.0.1", 11584, timeout=180)
    try:
        connection.request("GET" if data is None else "POST", path,
                           body=None if data is None else json.dumps(data),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Local inference returned HTTP {response.status}")
        return json.loads(response.read(1024 * 1024))
    finally:
        connection.close()


def prepared_cases(audit, scratch):
    rng = random.Random(SEED)
    for case in CASES:
        history = history_rows(case, "Reviewed both files. No implementation changes.")
        for arm in rng.sample(ARMS, len(ARMS)):
            room = scratch / f"{case.name}-{arm}"
            room.mkdir()
            mode = "unprompted_notes" if arm == "prompted_notes" else arm
            context, refs, *_ = prepare_arm(audit, room, case, mode, history, case.review, False)
            assert len(context) <= 4000
            yield case, arm, context, refs


def run(args, grader, scratch):
    canary = scratch / "outside.txt"
    canary.write_text("synthetic canary", encoding="utf-8")
    grader.verify_boundary(canary)
    for case in (*QUALIFY, *CASES):
        assert not evaluate(grader, case.original, case)
        assert evaluate(grader, case.reference, case)
    assert not (args.audit_root / "memory/task_library.py").exists()
    if args.context_only:
        scores = dict.fromkeys(ARMS, 0)
        for case, arm, context, refs in prepared_cases(args.audit_root, scratch):
            review = json.dumps(case.review, ensure_ascii=False)[1:-1]
            present = review in context or case.review in context
            scores[arm] += present
            emit("context", task=case.name, arm=arm, exact_decision=present,
                 context_chars=len(context), evidence_references=len(refs))
        assert scores["no_memory"] == 0 and scores["prompted_notes"] == len(CASES)
        emit("summary", cases=len(CASES), exact_decisions=scores, requests=0)
        return
    tags = local_request("/api/tags")["models"]
    if not any(tag["name"] == args.model and tag["digest"] == args.digest for tag in tags):
        raise RuntimeError("Pinned local model unavailable")
    cap = len(QUALIFY) + len(CASES) * len(ARMS)
    tokens = 768 if args.think else MODELS[args.model]
    requests = 0
    emit("environment", model=args.model, digest=args.digest, audit=AUDIT, seed=SEED, cap=cap,
         ollama=local_request("/api/version")["version"], reference_checks=len(QUALIFY) + len(CASES),
         review_prompt=args.review_prompt, output_cap=tokens, think=args.think)

    def infer(messages):
        nonlocal requests
        if requests >= cap:
            raise RuntimeError("Local request cap exceeded")
        requests += 1
        if args.review_prompt:
            messages = [{
                "role": "system",
                "content": "Review each stated requirement against both functions before replying. "
                           "Check boundary conditions, defaults, invalid inputs, and output shapes. "
                           "Then output only the two requested source files.",
            }, *messages]
        start = perf_counter()
        answer = local_request("/api/chat", {
            "model": args.model, "messages": messages, "stream": False, "keep_alive": "2m",
            **({"think": args.think} if args.model == "qwen3.5:2b" else {}),
            "options": {"temperature": 0, "seed": SEED, "num_ctx": 2048, "num_predict": tokens},
        })
        usage = {
            "input_tokens": answer.get("prompt_eval_count"),
            "output_tokens": answer.get("eval_count"),
            "elapsed_ms": round((perf_counter() - start) * 1000, 2),
            "done_reason": answer.get("done_reason"),
        }
        return "" if usage["done_reason"] == "length" else answer["message"]["content"], usage

    qualified = 0
    for case in QUALIFY:
        result = ask(infer, grader, case, case.review)
        qualified += result["passed"]
        emit("qualification", task=case.name, **result)
    scores = dict.fromkeys(ARMS, 0)
    if qualified == len(QUALIFY):
        for case, arm, context, _ in prepared_cases(args.audit_root, scratch):
            result = ask(infer, grader, case, context)
            scores[arm] += result["passed"]
            emit("scored", task=case.name, arm=arm, **result)
    emit("summary", qualification_passed=qualified, requests=requests,
         scored=requests - len(QUALIFY), passes=scores if qualified == len(QUALIFY) else {})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", required=True, type=Path)
    parser.add_argument("--wasi-root", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--model", choices=MODELS)
    parser.add_argument("--digest")
    parser.add_argument("--context-only", action="store_true", help="audit prepared prompts without model inference")
    parser.add_argument("--review-prompt", action="store_true", help="use the same review instruction in every arm")
    parser.add_argument("--think", action="store_true", help="allow a longer reasoning budget for Qwen3.5")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run with assertions enabled")
    if not args.context_only and not (args.model and args.digest):
        parser.error("Provide --model and --digest to run inference")
    if args.think and args.model != "qwen3.5:2b":
        parser.error("--think requires qwen3.5:2b")
    args.audit_root = args.audit_root.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="trace-preview-coding-", dir=args.scratch) as path:
        run(args, Grader(args.wasi_root.resolve(strict=True)), Path(path))


if __name__ == "__main__":
    main()
