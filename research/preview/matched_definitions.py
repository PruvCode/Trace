"""Fresh matched coding comparison with the same editor and public tests in both arms."""

import argparse
import ast
import json
from contextlib import closing
from math import comb
from pathlib import Path
from random import Random
from statistics import median, quantiles
from tempfile import TemporaryDirectory
from time import perf_counter

from memory import episodic, session_context, store, task_library, transcript
from research.phase5.grader import Grader
from research.preview import effectiveness as harness
from research.preview.definition_trial import function_only
from research.preview.trace2_evaluation import DIGEST, QWEN, snapshot
from research.preview.verification import diagnostic
from research.production.multi_file import Case, evaluate
from trace_memory import project


# Checks start with two public inputs per definition. The rest never enter inference.
CASES = (
    Case("parking", "Apply the agreed parking rule.",
         "billable returns the number of started hours after a 10-minute grace period, with a minimum of zero. "
         "charge uses billable and returns {'hours': hours, 'cents': hours times 240}, capped at 960 cents.",
         ("def billable(minutes):\n    return minutes // 60", "from protocol import billable\ndef charge(minutes):\n    return billable(minutes) * 200"),
         ("def billable(minutes):\n    return max(0, (minutes - 10 + 59) // 60)",
          "from protocol import billable\ndef charge(minutes):\n    hours = billable(minutes)\n    return {'hours': hours, 'cents': min(960, hours * 240)}"),
         (("protocol.billable(11)", 1), ("protocol.billable(10)", 0), ("handler.charge(800)", {"hours": 14, "cents": 960}),
          ("handler.charge(14)", {"hours": 1, "cents": 240}), ("protocol.billable(70)", 1),
          ("handler.charge(131)", {"hours": 3, "cents": 720}), ("handler.charge(0)", {"hours": 0, "cents": 0}))),
    Case("roster", "Implement the approved staff roster policy.",
         "role trims and lowercases its input. It returns 'lead' or 'staff' for those values and None otherwise. "
         "roster returns a dictionary with 'lead' and 'staff' lists of IDs, dropping unknown roles and preserving order within each list.",
         ("def role(value):\n    return value", "from protocol import role\ndef roster(rows):\n    return {key: role(kind) for key, kind in rows}"),
         ("def role(value):\n    value = value.strip().lower()\n    return value if value in ('lead', 'staff') else None",
          "from protocol import role\ndef roster(rows):\n    groups = {'lead': [], 'staff': []}\n    for key, kind in rows:\n        name = role(kind)\n        if name is not None:\n            groups[name].append(key)\n    return groups"),
         (("protocol.role(' LEAD ')", "lead"), ("protocol.role('guest')", None), ("handler.roster([])", {"lead": [], "staff": []}),
          ("handler.roster([(3, ' Lead'), (2, 'unknown')])", {"lead": [3], "staff": []}), ("protocol.role(' STAFF  ')", "staff"),
          ("handler.roster([(8,'staff'),(5,'lead'),(4,'Staff'),(6,'')])", {"lead": [5], "staff": [8, 4]}))),
    Case("shipment", "Apply the approved shipment limit policy.",
         "capacity returns 24 for express and 48 for standard, and zero for all other methods. "
         "manifest returns None if the amount is nonpositive or exceeds capacity. Otherwise return {'amount': amount, 'space': capacity minus amount}.",
         ("def capacity(method):\n    return 12", "from protocol import capacity\ndef manifest(method, amount):\n    return min(amount, capacity(method))"),
         ("def capacity(method):\n    return {'express': 24, 'standard': 48}.get(method, 0)",
          "from protocol import capacity\ndef manifest(method, amount):\n    limit = capacity(method)\n    if 0 < amount <= limit:\n        return {'amount': amount, 'space': limit - amount}"),
         (("protocol.capacity('express')", 24), ("protocol.capacity('courier')", 0), ("handler.manifest('express', 24)", {"amount": 24, "space": 0}),
          ("handler.manifest('standard', 0)", None), ("protocol.capacity('standard')", 48), ("handler.manifest('courier', 1)", None),
          ("handler.manifest('standard', 40)", {"amount": 40, "space": 8}), ("handler.manifest('express', 25)", None))),
    Case("attendance", "Apply the agreed attendance classification.",
         "attendance maps 'p' to 'present', 'a' to 'absent', 'e' to 'excused', and unknown codes to None. "
         "total returns the count of present students and the number of scored students as a tuple. Only present and absent count as scored.",
         ("def attendance(code):\n    return code", "from protocol import attendance\ndef total(codes):\n    return len(codes)"),
         ("def attendance(code):\n    return {'p': 'present', 'a': 'absent', 'e': 'excused'}.get(code)",
          "from protocol import attendance\ndef total(codes):\n    rows = [attendance(code) for code in codes]\n    return rows.count('present'), rows.count('present') + rows.count('absent')"),
         (("protocol.attendance('a')", "absent"), ("protocol.attendance('x')", None), ("handler.total(['p','e','a'])", (1, 2)),
          ("handler.total([])", (0, 0)), ("protocol.attendance('e')", "excused"), ("protocol.attendance('p')", "present"),
          ("handler.total(['p','p','?','e','a','a','e'])", (2, 4)))),
    Case("library", "Apply the approved library fines.",
         "rate returns 25 for a book, 100 for a disc and zero for unknown items. "
         "fine charges for positive days after a two-day grace period at that rate, capped at 500 cents. Return a whole number of cents.",
         ("def rate(item):\n    return 50", "from protocol import rate\ndef fine(item, days):\n    return days * rate(item)"),
         ("def rate(item):\n    return {'book': 25, 'disc': 100}.get(item, 0)",
          "from protocol import rate\ndef fine(item, days):\n    return min(500, max(0, days - 2) * rate(item))"),
         (("protocol.rate('disc')", 100), ("protocol.rate('map')", 0), ("handler.fine('disc', 9)", 500), ("handler.fine('book', 2)", 0),
          ("protocol.rate('book')", 25), ("handler.fine('book', 5)", 75), ("handler.fine('other', 10)", 0), ("handler.fine('disc', 3)", 100))),
    Case("kitchen", "Apply the approved kitchen portion rules.",
         "portion maps 'small', 'regular', and 'large' to 2, 3, and 5 respectively. Use 3 for an unknown size. "
         "prepare returns a list with each order's portion count in original order, retaining zero when the order count is nonpositive.",
         ("def portion(size):\n    return 1", "from protocol import portion\ndef prepare(orders):\n    return sum(portion(size) * count for size, count in orders)"),
         ("def portion(size):\n    return {'small': 2, 'regular': 3, 'large': 5}.get(size, 3)",
          "from protocol import portion\ndef prepare(orders):\n    return [portion(size) * max(0, count) for size, count in orders]"),
         (("protocol.portion('large')", 5), ("protocol.portion('other')", 3), ("handler.prepare([('small', 2), ('large', -1)])", [4, 0]),
          ("handler.prepare([])", []), ("protocol.portion('small')", 2), ("handler.prepare([('other', 3), ('large', 1), ('regular', 0)])", [9, 5, 0]))),
    Case("tickets", "Apply the agreed venue ticket rules.",
         "cost maps 'balcony' to 18 and 'floor' to 32, and returns None for unknown seats. "
         "checkout returns None for unknown seats or a count outside 1 through 6. Otherwise return {'subtotal': count times cost, 'fee': 4}.",
         ("def cost(seat):\n    return 10", "from protocol import cost\ndef checkout(seat, count):\n    return count * cost(seat)"),
         ("def cost(seat):\n    return {'balcony': 18, 'floor': 32}.get(seat)",
          "from protocol import cost\ndef checkout(seat, count):\n    price = cost(seat)\n    if price is not None and 1 <= count <= 6:\n        return {'subtotal': price * count, 'fee': 4}"),
         (("protocol.cost('floor')", 32), ("protocol.cost('lawn')", None), ("handler.checkout('balcony', 6)", {"subtotal": 108, "fee": 4}),
          ("handler.checkout('floor', 0)", None), ("protocol.cost('balcony')", 18), ("handler.checkout('floor', 7)", None),
          ("handler.checkout('lawn', 1)", None), ("handler.checkout('floor', 4)", {"subtotal": 128, "fee": 4}))),
    Case("schedule", "Apply the approved appointment rounding rules.",
         "slot rounds a nonnegative minute down to its 15-minute boundary. Negative minutes return None. "
         "schedule returns sorted unique slots for all valid minutes, including zero.",
         ("def slot(minute):\n    return minute", "from protocol import slot\ndef schedule(minutes):\n    return list(map(slot, minutes))"),
         ("def slot(minute):\n    return minute // 15 * 15 if minute >= 0 else None",
          "from protocol import slot\ndef schedule(minutes):\n    slots = map(slot, minutes)\n    return sorted({value for value in slots if value is not None})"),
         (("protocol.slot(16)", 15), ("protocol.slot(-1)", None), ("handler.schedule([31, 1, 16, 29, -3])", [0, 15, 30]),
          ("handler.schedule([])", []), ("protocol.slot(45)", 45), ("handler.schedule([14, 15, 44, 30, -1])", [0, 15, 30]))),
)

ARMS = ("notes", "trace")
SEED = 20381
GEMMA = "gemma3:4b"
GEMMA_DIGEST = "a2af6cc3eb7fa8be8504abaf9b04e88f17a119ec3f04a3addf55f92841195f5a"
QUALIFICATION = (
    Case("qualify-range", "Apply the approved value range.",
         "limit returns 12. keep returns True only for nonnegative values up to limit, inclusive.",
         ("def limit():\n    return 9", "from protocol import limit\ndef keep(value):\n    return value < limit()"),
         ("def limit():\n    return 12", "from protocol import limit\ndef keep(value):\n    return 0 <= value <= limit()"),
         (("protocol.limit()", 12), ("protocol.limit()", 12), ("handler.keep(12)", True),
          ("handler.keep(-1)", False), ("handler.keep(0)", True), ("handler.keep(13)", False))),
    Case("qualify-label", "Apply the approved label format.",
         "label strips surrounding spaces and uppercases its input. emit returns a dictionary with one key 'title' and the label value.",
         ("def label(value):\n    return value", "from protocol import label\ndef emit(value):\n    return {'name': label(value)}"),
         ("def label(value):\n    return value.strip().upper()", "from protocol import label\ndef emit(value):\n    return {'title': label(value)}"),
         (("protocol.label('abc')", "ABC"), ("protocol.label(' z ')", "Z"),
          ("handler.emit('ab')", {"title": "AB"}), ("handler.emit(' hi')", {"title": "HI"}),
          ("handler.emit(' x y ')", {"title": "X Y"}))),
)


def definitions(case, room):
    snapshot(room, case)
    project.init_project(room)
    db = project.db_path(room)
    with closing(store.connect(db)) as conn:
        task = task_library.create_task(conn, case.name)["id"]
        evidence = transcript.record_message(conn, session_id="review", role="user", content=case.review)
        event = episodic.record_event(conn, repo="fixture", type="decision", task_id=task,
                                      payload={"decision": case.review, "status": "verified"},
                                      evidence=[{"type": "transcript", "message_id": evidence}])
    result = session_context.get_session_start_context(db, task_id=task, project_root=room, query=case.task)
    item = next(item for item in result["items"] if item.get("event_id") == event)
    assert item["text"] == case.review and item["evidence"]
    return {target["path"]: target for target in item.get("code_targets", ())}


def edit(grader, case, facts, model):
    sources = list(case.original)
    calls = tokens = generated = 0
    for index, (path, source) in enumerate(zip(harness.FILES, case.original)):
        body = ast.parse(source).body
        definition = next(node for node in body if isinstance(node, ast.FunctionDef))
        imports = [ast.get_source_segment(source, node) for node in body if isinstance(node, (ast.Import, ast.ImportFrom))]
        checks = case.checks[2 * index:2 * index + 2]
        examples = "\n".join(f"{call} -> {expected!r}" for call, expected in checks)
        prompt = (f"{case.task}\nWrite the replacement for {definition.name} in {path}.\n"
                  f"Approved behavior:\n{facts[path]}\n\nPublic examples:\n{examples}\n\n"
                  f"Current file:\n{source}\n\nOther module:\n{sources[1-index]}\n\n"
                  f"Output one Python def with the same name and signature. You may use the existing imports. "
                  "Include no example calls or new imports.")
        for attempt in range(2):
            calls += 1
            try:
                reply = harness.request("/api/chat", {"model": model, "stream": False, "keep_alive": "5m",
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0, "seed": SEED, "num_ctx": 2048, "num_predict": 230}})
            except TimeoutError:
                continue
            tokens += reply.get("prompt_eval_count") or 0
            generated += reply.get("eval_count") or 0
            replacement = function_only(reply["message"]["content"], definition.name, source)
            if reply.get("done_reason") == "length" or replacement is None:
                prompt += "\nThe last reply did not yield a single complete function with the unchanged signature."
                continue
            candidate = list(sources)
            candidate[index] = "\n".join([*imports, replacement])
            failures = [diagnostic(grader, candidate, check) for check in checks]
            if not any(failures):
                sources = candidate
                break
            if attempt == 0:
                prompt += "\nPrevious proposal:\n" + replacement + "\n" + "\n".join(filter(None, failures))
    return sources, calls, tokens, generated


def run(grader, scratch, model, resume=None, *, digest=DIGEST):
    canary = scratch / "canary"
    canary.write_text("synthetic host canary", encoding="utf-8")
    grader.verify_boundary(canary)
    fixture = []
    for case in CASES:
        assert not evaluate(grader, case.original, case) and evaluate(grader, case.reference, case)
        for index, path in enumerate(harness.FILES):
            symbol = next(node.name for node in ast.parse(case.original[index]).body if isinstance(node, ast.FunctionDef))
            assert all(call.startswith(f"{Path(path).stem}.{symbol}(") for call, _ in case.checks[2 * index:2 * index + 2])
        targets = definitions(case, scratch / case.name)
        assert set(targets) == set(harness.FILES)
        for path, target in targets.items():
            assert target["decision_excerpt"] in case.review
            assert target["symbol"] in case.original[harness.FILES.index(path)]
        fixture.append((case, targets))
    harness.emit("preflight", tasks=len(fixture), source_locations=len(fixture) * 2)
    if model is None:
        return
    assert any(row["name"] == model and row["digest"] == digest for row in harness.request("/api/tags")["models"])
    harness.emit("environment", model=model, digest=digest, seed=SEED, max_calls_per_task=4,
                 ollama=harness.request("/api/version")["version"], inference_cost_usd=0)
    rng = Random(SEED)
    results = {arm: [] for arm in ARMS}
    saved = {}
    if resume:
        rows = [json.loads(line) for line in resume.read_text(encoding="utf-8-sig").splitlines()]
        assert any(row["type"] == "environment" and row["digest"] == digest and row["seed"] == SEED for row in rows)
        saved = {(row["task"], row["arm"]): row for row in rows if row["type"] == "scored"}
    for case, targets in fixture:
        for arm in rng.sample(ARMS, len(ARMS)):
            if (case.name, arm) in saved:
                row = saved[case.name, arm]
                assert evaluate(grader, harness.valid_sources(row["sources"]), case) == row["passed"]
                results[arm].append(row)
                harness.emit("scored", **{key: value for key, value in row.items() if key != "type"})
                continue
            facts = {path: targets[path]["decision_excerpt"] if arm == "trace" else case.review for path in harness.FILES}
            start = perf_counter()
            sources, calls, tokens, generated = edit(grader, case, facts, model)
            row = dict(task=case.name, arm=arm, passed=evaluate(grader, sources, case), sources=sources,
                       calls=calls, input_tokens=tokens, output_tokens=generated, elapsed_ms=round((perf_counter() - start) * 1000))
            results[arm].append(row)
            harness.emit("scored", **row)
    harness.emit("summary", cases=len(CASES), arms={arm: {
        "passed": sum(row["passed"] for row in rows), "calls": sum(row["calls"] for row in rows),
        "median_input_tokens": median(row["input_tokens"] for row in rows),
        "median_elapsed_ms": median(row["elapsed_ms"] for row in rows),
    } for arm, rows in results.items()})


def regrade(grader, path):
    cases = {case.name: case for case in CASES}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()]
    scored = [row for row in rows if row["type"] == "scored"]
    assert len(scored) == len(cases) * len(ARMS) and rows[-1]["type"] == "summary"
    verdicts = {}
    for row in scored:
        case = cases[row["task"]]
        key = (case.name, row["arm"])
        assert key not in verdicts and row["arm"] in ARMS
        verdict = evaluate(grader, harness.valid_sources(row["sources"]), case)
        assert verdict == row["passed"], key
        verdicts[key] = verdict
    differences = [int(verdicts[name, "trace"]) - int(verdicts[name, "notes"]) for name in cases]
    rng = Random(SEED)
    samples = [sum(rng.choices(differences, k=len(cases))) / len(cases) for _ in range(10000)]
    interval = quantiles(samples, n=40)
    wins, losses = differences.count(1), differences.count(-1)
    discordant = wins + losses
    exact_p = min(1.0, 2 * sum(comb(discordant, count) for count in range(min(wins, losses) + 1)) / 2**discordant)
    harness.emit("regrade", verdicts=len(verdicts), disagreements=0,
                 trace_wins=wins, notes_wins=losses, ties=differences.count(0),
                 lift=sum(differences) / len(cases), paired_bootstrap_95=[interval[0], interval[-1]],
                 paired_exact_p=exact_p)


def qualify(grader, model, digest):
    assert any(row["name"] == model and row["digest"] == digest for row in harness.request("/api/tags")["models"])
    passed = 0
    for case in QUALIFICATION:
        assert not evaluate(grader, case.original, case) and evaluate(grader, case.reference, case)
        sources, calls, tokens, generated = edit(grader, case, dict.fromkeys(harness.FILES, case.review), model)
        verdict = evaluate(grader, sources, case)
        passed += verdict
        harness.emit("qualification", task=case.name, model=model, passed=verdict, sources=sources,
                     calls=calls, input_tokens=tokens, output_tokens=generated)
    harness.emit("qualification_summary", model=model, digest=digest, passed=passed, required=len(QUALIFICATION))
    return passed == len(QUALIFICATION)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wasi", type=Path, required=True)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--regrade", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--qualify-gemma", action="store_true")
    parser.add_argument("--gemma", action="store_true")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run with assertions enabled")
    if not args.regrade and not args.scratch and not args.qualify_gemma:
        parser.error("Provide --scratch for a trial run")
    grader = Grader(args.wasi.resolve(strict=True))
    if args.regrade:
        regrade(grader, args.regrade)
    elif args.qualify_gemma:
        qualify(grader, GEMMA, GEMMA_DIGEST)
    else:
        model, digest = (GEMMA, GEMMA_DIGEST) if args.gemma else (QWEN, DIGEST)
        if args.gemma and not args.preflight_only and not qualify(grader, model, digest):
            raise SystemExit(1)
        with TemporaryDirectory(prefix="trace-match-", dir=args.scratch.resolve(strict=True)) as folder:
            run(grader, Path(folder), None if args.preflight_only else model, args.resume, digest=digest)
