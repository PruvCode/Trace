"""Fresh two-file coding trials with matched public feedback and hidden grading."""

from argparse import ArgumentParser
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from random import Random
from statistics import median
from tempfile import TemporaryDirectory
import json
import secrets

from memory import captured_outcomes, session_context, store, synthetic
from research.phase5.grader import Grader
from research.preview import effectiveness as harness
from research.preview.trace2_evaluation import DIGEST, QWEN, project_block, snapshot
from research.production.multi_file import QUALIFY, SEED, Case, evaluate, load_sources

ARMS = ("no_memory", "notes", "trace")
BOUNDARY_PROBES = {
    "warehouse": (("protocol.aisle(' ice ')", "cold"), ("handler.pick(' ice ', 3)", {"aisle": "cold", "quantity": 3})),
    "subscription": (("handler.renewal('expired', 10)", None), ("handler.renewal('standard', -7)", None)),
    "deduplication": (("protocol.normalized(' STRAẞE ')", "strasse"),
                      ("handler.ingest([' Z ', 'z', 'A', ''])", ["Z", "A"])),
    "inventory": (("protocol.available(2)", 0), ("handler.allocate(10, 7)", {"allocated": 7, "remaining": 3})),
}
CASES = (
    Case("warehouse", "Update aisle and pick for the approved warehouse routing policy.",
         "aisle strips and lowercases zone. Zone 'ice' routes to 'cold', 'bulk' to 'freight', others to None. "
         "pick skips unknown routes and nonpositive quantities. Otherwise return {'aisle': route, 'quantity': qty}.",
         ("def aisle(zone):\n    return zone.lower()", "from protocol import aisle\ndef pick(zone, qty):\n    return (aisle(zone), qty)"),
         ("def aisle(zone):\n    return {'ice': 'cold', 'bulk': 'freight'}.get(zone.strip().lower())",
          "from protocol import aisle\ndef pick(zone, qty):\n    route = aisle(zone)\n"
          "    return {'aisle': route, 'quantity': qty} if route is not None and qty > 0 else None"),
         (("handler.pick(' ICE ', 2)", {"aisle": "cold", "quantity": 2}), ("protocol.aisle('other')", None),
          ("protocol.aisle(' Bulk ')", "freight"), ("handler.pick('bulk', 0)", None),
          ("handler.pick('other', 4)", None), ("handler.pick('ice', -2)", None))),
    Case("subscription", "Update cycle_days and renewal for the approved subscription rules.",
         "cycle_days returns 14 for 'trial' and 60 for 'standard'. Other plans return None. "
         "renewal returns None for an unknown plan or negative start. Otherwise return "
         "{'due': start plus cycle_days(plan), 'plan': plan}. Treat start zero as valid.",
         ("def cycle_days(plan):\n    return 30", "from protocol import cycle_days\ndef renewal(plan, start):\n    return start + cycle_days(plan)"),
         ("def cycle_days(plan):\n    return {'trial': 14, 'standard': 60}.get(plan)",
          "from protocol import cycle_days\ndef renewal(plan, start):\n    days = cycle_days(plan)\n"
          "    return None if start < 0 or days is None else {'due': start + days, 'plan': plan}"),
         (("handler.renewal('trial', 0)", {"due": 14, "plan": "trial"}), ("protocol.cycle_days('standard')", 60),
          ("protocol.cycle_days('other')", None), ("handler.renewal('trial', -1)", None),
          ("handler.renewal('other', 3)", None), ("handler.renewal('standard', 10)", {"due": 70, "plan": "standard"}))),
    Case("deduplication", "Update normalized and ingest using the approved deduplication policy.",
         "normalized strips surrounding whitespace and uses casefold. ingest returns the first trimmed original "
         "spelling of each distinct normalized value, preserving input order and dropping empty values.",
         ("def normalized(name):\n    return name.lower()", "from protocol import normalized\ndef ingest(names):\n    return sorted(set(map(normalized, names)))"),
         ("def normalized(name):\n    return name.strip().casefold()",
          "from protocol import normalized\ndef ingest(names):\n    seen, result = set(), []\n"
          "    for name in names:\n        key = normalized(name)\n        if key and key not in seen:\n"
          "            seen.add(key)\n            result.append(name.strip())\n    return result"),
         (("handler.ingest([' B ', 'a', 'b', ' '])", ["B", "a"]), ("protocol.normalized(' Straße ')", "strasse"),
          ("handler.ingest([' Straße ', 'STRASSE', 'z'])", ["Straße", "z"]), ("handler.ingest([])", []),
          ("handler.ingest(['B', 'a', 'b', 'A', 'c'])", ["B", "a", "c"]))),
    Case("calendar", "Update day_class and reserve to match the approved calendar rules.",
         "day_class returns 'weekend' for days 5 and 6, 'weekday' for 0 through 4, and None otherwise. "
         "reserve returns False for invalid days or minutes <= 0. A weekday allows up to 90 minutes inclusive. "
         "A weekend allows up to 45 minutes inclusive.",
         ("def day_class(day):\n    return 'weekday'", "from protocol import day_class\ndef reserve(day, minutes):\n    return minutes < 60"),
         ("def day_class(day):\n    if 0 <= day <= 6:\n        return 'weekend' if day >= 5 else 'weekday'",
          "from protocol import day_class\ndef reserve(day, minutes):\n    label = day_class(day)\n"
          "    return label is not None and 0 < minutes <= (45 if label == 'weekend' else 90)"),
         (("handler.reserve(5, 46)", False), ("protocol.day_class(6)", "weekend"),
          ("protocol.day_class(-1)", None), ("handler.reserve(6, 45)", True), ("handler.reserve(7, 1)", False),
          ("handler.reserve(2, 90)", True), ("handler.reserve(0, 0)", False))),
    Case("telemetry", "Update band and summarize for the approved sensor report.",
         "band returns 'low' for readings below 20, 'high' above 80, and 'normal' otherwise. "
         "summarize skips None values and counts all three bands, returning all three keys even when their count is zero.",
         ("def band(value):\n    return 'high' if value > 50 else 'low'", "from protocol import band\ndef summarize(values):\n    return [band(value) for value in values]"),
         ("def band(value):\n    return 'low' if value < 20 else 'high' if value > 80 else 'normal'",
          "from protocol import band\ndef summarize(values):\n    counts = dict(low=0, normal=0, high=0)\n"
          "    for value in values:\n        if value is not None:\n            counts[band(value)] += 1\n    return counts"),
         (("handler.summarize([None, 10, 85])", {"low": 1, "normal": 0, "high": 1}),
          ("protocol.band(20)", "normal"), ("protocol.band(80)", "normal"),
          ("handler.summarize([])", {"low": 0, "normal": 0, "high": 0}),
          ("handler.summarize([19, 20, 81, 81, None])", {"low": 1, "normal": 1, "high": 2}))),
    Case("inventory", "Update available and allocate for the approved stock policy.",
         "available returns stock minus a reserve of 3, floored at zero. allocate returns None for requests "
         "below 1 or above available(stock). Otherwise return {'allocated': requested, 'remaining': stock minus requested}. "
         "The remaining value includes the reserve.",
         ("def available(stock):\n    return stock", "from protocol import available\ndef allocate(stock, requested):\n    return min(available(stock), requested)"),
         ("def available(stock):\n    return max(0, stock - 3)",
          "from protocol import available\ndef allocate(stock, requested):\n"
          "    if 1 <= requested <= available(stock):\n        return {'allocated': requested, 'remaining': stock - requested}"),
         (("handler.allocate(8, 5)", {"allocated": 5, "remaining": 3}), ("protocol.available(1)", 0),
          ("handler.allocate(3, 1)", None), ("handler.allocate(8, 6)", None), ("handler.allocate(8, 0)", None),
          ("handler.allocate(21, 4)", {"allocated": 4, "remaining": 17}))),
)


def context(case, arm, room, *, with_items=False):
    room = room.resolve()
    snapshot(room, case)
    if arm == "no_memory":
        return ""
    test_path = "test_contract.py"
    node = f"{test_path}::test_{case.name}_contract"
    if arm == "notes":
        return f"{case.review}\nPreviously failing check to rerun: {node}"
    (room / test_path).touch()
    task, event, options, db = project_block(room, case, contested=True)
    with closing(store.connect(db)) as conn:
        since = captured_outcomes.configure(conn, "on")["enabled_since_ms"]
        part = {"type": "tool", "tool": "bash", "state": {"status": "completed", "input": {"command": "pytest -q"},
                "metadata": {"exit": 1}, "output": f"=== short test summary info ===\nFAILED {node}\n1 failed in 0.01s"}}
        assert captured_outcomes.record(conn, True, project=room, repo="fixture", session_id="history-1",
                                        message_id="msg-1", tool_id="test-1", created_ms=since + 1, part=part, v2=False)
    result = session_context.get_session_start_context(db, **options)
    block = synthetic.render_user_block(result["items"])
    assert case.review in block and node in block and "SUPERSEDED_FIXTURE" not in block
    assert any(item.get("event_id") == event and item.get("evidence") for item in result["items"])
    return (block, result["items"]) if with_items else block


def diagnostic(grader, sources, check):
    if sources is None:
        return "The response did not contain two valid Python files."
    call, expected = check
    token = secrets.token_hex(16)
    ok, observed = grader.run(f"{load_sources(sources)}result = {call}\n"
                              f"print({token!r} if result == {expected!r} and type(result) is type({expected!r}) else repr(result))")
    if ok and observed == (token + "\n").encode():
        return ""
    actual = ascii(observed.decode("utf-8", errors="replace").strip()[:160]) if ok else "execution failed"
    return f"Public assertion {call} expected {expected!r}. Observed repr: {actual}."


def run(grader, scratch, preflight_only):
    canary = scratch / "outside.txt"
    canary.write_text("synthetic filesystem boundary", encoding="utf-8")
    grader.verify_boundary(canary)
    for case in (*QUALIFY, *CASES):
        assert not evaluate(grader, case.original, case) and evaluate(grader, case.reference, case)
        assert diagnostic(grader, case.original, case.checks[0])
        assert not diagnostic(grader, case.reference, case.checks[0])
    rng = Random(SEED)
    trials = [(case, arm, context(case, arm, scratch / f"{case.name}-{arm}"))
              for case in CASES for arm in rng.sample(ARMS, len(ARMS))]
    harness.emit("preflight", originals_fail=len(CASES), references_pass=len(CASES), trace_contexts=len(CASES))
    if preflight_only:
        return
    harness.MODEL, harness.DIGEST = QWEN, DIGEST
    harness.MAX_REQUESTS = len(QUALIFY) + len(trials) * 2
    local = harness.LocalRun()
    for case in QUALIFY:
        result = local.grade(grader, case, case.review)
        harness.emit("qualification", task=case.name, **result)
        if not result["passed"]:
            raise RuntimeError("Model did not qualify")
    scores = {arm: [] for arm in ARMS}
    for case, arm, block in trials:
        public = replace(case, checks=case.checks[:1])
        initial = local.grade(grader, public, block)
        harness.emit("first_attempt", task=case.name, arm=arm, **initial)
        public_failure = diagnostic(grader, initial["sources"], case.checks[0]) if not initial["passed"] else ""
        result = initial
        if not initial["passed"]:
            feedback = f"\n\nPrevious candidate (JSON)\n{json.dumps(initial['sources'])}\n\n{public_failure}\nReturn both corrected files."
            result = local.grade(grader, public, block, feedback=feedback)
        attempts = [initial] if result is initial else [initial, result]
        row = {"initial_pass": evaluate(grader, initial["sources"], case),
               "final_pass": evaluate(grader, result["sources"], case), "retries": int(not initial["passed"]),
               "public_feedback": public_failure, "final_sources": result["sources"],
               "input_tokens": sum(r["input_tokens"] or 0 for r in attempts),
               "model_ms": sum(r["elapsed_ms"] for r in attempts)}
        scores[arm].append(row)
        harness.emit("scored", task=case.name, arm=arm, **row)
    harness.emit("summary", requests=local.requests, arms={arm: {
        "first_pass": sum(row["initial_pass"] for row in rows), "final_pass": sum(row["final_pass"] for row in rows),
        "retries": sum(row["retries"] for row in rows), "median_input_tokens": median(row["input_tokens"] for row in rows),
        "median_model_ms": median(row["model_ms"] for row in rows),
    } for arm, rows in scores.items()})


def regrade(grader, path):
    cases = {case.name: case for case in (*QUALIFY, *CASES)}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()]
    starts = {(row["task"], row["arm"]): row for row in rows if row["type"] == "first_attempt"}
    scored = {(row["task"], row["arm"]): row for row in rows if row["type"] == "scored"}
    expected = {(case.name, arm) for case in CASES for arm in ARMS}
    assert starts.keys() == scored.keys() == expected and rows[-1]["type"] == "summary"
    for (name, arm), row in scored.items():
        case = cases[name]
        start = starts[name, arm]
        assert evaluate(grader, harness.valid_sources(start["sources"]), case) == row["initial_pass"], (name, arm)
        assert evaluate(grader, harness.valid_sources(row["final_sources"]), case) == row["final_pass"], (name, arm)
    harness.emit("regrade", trials=len(scored), candidate_verdicts=len(scored) * 2, disagreements=0)


def boundary_followup(grader, path, scratch):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()]
    assert rows[-1]["type"] == "summary"
    starts = {(row["task"], row["arm"]): row for row in rows if row["type"] == "first_attempt"}
    rng = Random(SEED)
    trials = []
    for case in CASES:
        if case.name not in BOUNDARY_PROBES:
            continue
        probes = (*case.checks[:1], *BOUNDARY_PROBES[case.name])
        public = replace(case, checks=probes)
        assert evaluate(grader, case.reference, public)
        assert all(probe not in case.checks for probe in probes[1:])
        for arm in rng.sample(("notes", "trace"), 2):
            start = starts[case.name, arm]
            sources = harness.valid_sources(start["sources"])
            failures = [failure for probe in probes if (failure := diagnostic(grader, sources, probe))]
            trials.append((case, public, arm, sources, list(dict.fromkeys(failures))))
    harness.MODEL, harness.DIGEST = QWEN, DIGEST
    harness.MAX_REQUESTS = len(trials)
    local = harness.LocalRun()
    for case, public, arm, sources, failures in trials:
        if failures:
            block = context(case, arm, scratch / f"{case.name}-{arm}")
            feedback = (f"\n\nPrevious candidate (JSON)\n{json.dumps(sources)}\n\n"
                        + "\n".join(failures)
                        + "\nFix each failure in the function that owns that rule. Check the approved rules again. Return both corrected files.")
            result = local.grade(grader, public, block, feedback=feedback)
            sources = result["sources"]
        else:
            result = {"passed": True, "input_tokens": 0, "output_tokens": 0, "elapsed_ms": 0}
        harness.emit("followup", task=case.name, arm=arm, public_failures=failures,
                     public_pass=result["passed"], hidden_pass=evaluate(grader, sources, case),
                     sources=sources, input_tokens=result["input_tokens"], elapsed_ms=result["elapsed_ms"])
    harness.emit("followup_summary", requests=local.requests, trials=len(trials))


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--wasi", type=Path, required=True)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--regrade", type=Path)
    parser.add_argument("--boundary-followup", type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run with assertions enabled")
    if not args.regrade and not args.scratch:
        parser.error("Provide --scratch for a trial run.")
    grader = Grader(args.wasi.resolve(strict=True))
    if args.regrade:
        regrade(grader, args.regrade)
    else:
        with TemporaryDirectory(prefix="trace-verify-", dir=args.scratch.resolve(strict=True)) as folder:
            if args.boundary_followup:
                boundary_followup(grader, args.boundary_followup, Path(folder))
            else:
                run(grader, Path(folder), args.preflight_only)
