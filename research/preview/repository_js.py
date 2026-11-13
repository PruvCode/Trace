"""Matched local edits on pinned Rubaru and WISIT source functions."""

import argparse
from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
from random import Random
import re
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter

from memory import episodic, session_context, store, task_library, transcript
from research.preview import effectiveness as harness
from research.preview.trace2_evaluation import DIGEST, QWEN
from trace_memory import project


@dataclass(frozen=True)
class Fixture:
    name: str
    repo: str
    revision: str
    path: str
    decision: str
    reference: dict
    public: dict
    hidden: tuple


RUBARU = "9246629f19fe742b4f5ae7959400b108fbc889c9"
WISIT = "c3ba53c27ee4382560d099a759525c53b52c68c4"
CASES = (
    Fixture("motion-segments", "rubaru-memory-pilot", RUBARU, "packages/art/src/core/math.ts",
            "seg treats an equal start and end as a step, returning zero below the boundary and one at or above it. "
            "bump returns zero on equal or reversed bounds. For forward bounds preserve the existing sine pulse.",
            {"seg": "if (a === b) return p < a ? 0 : 1; return clamp((p - a) / (b - a), 0, 1);",
             "bump": "if (b <= a) return 0; const t = seg(p, a, b); return t <= 0 || t >= 1 ? 0 : Math.sin(t * Math.PI);"},
            {"seg": (("seg(3,3,3)", 1), ("seg(2,3,3)", 0)), "bump": (("bump(4,5,3)", 0), ("bump(3,2,4)", 1))},
            (("seg(-5,-4,-4)", 0), ("seg(-3,-4,-4)", 1), ("seg(4,3,5)", 0.5),
             ("bump(-4,-4,-4)", 0), ("bump(7,10,4)", 0), ("bump(12,10,14)", 1), ("lerp(2,4,0.5)", 3))),
    Fixture("motion-sampling", "rubaru-memory-pilot", RUBARU, "packages/art/src/core/math.ts",
            "stepQ returns zero when the step count is below one. For valid counts, floor the count to an integer before quantizing. "
            "cruiseE returns clamped linear progress when either ramp is negative or their sum exceeds one. Preserve valid ramps.",
            {"stepQ": "n = Math.floor(n); return n >= 1 ? Math.floor(clamp(t, 0, 1) * n) / n : 0;",
             "cruiseE": "t = clamp(t, 0, 1); if (a < 0 || b < 0 || a + b > 1) return t; "
             "const k = 1 / (1 - a / 2 - b / 2); let x; "
             "if (t < a) x = t * t / (2 * a); else if (t <= 1 - b) x = a / 2 + (t - a); "
             "else { const u = t - (1 - b); x = a / 2 + (1 - b - a) + u - u * u / (2 * b); } return x * k;"},
            {"stepQ": (("stepQ(0.6,0)", 0), ("stepQ(0.7,2.5)", 0.5)),
             "cruiseE": (("cruiseE(0.4,0.8,0.8)", 0.4), ("cruiseE(0.3,-0.2,0.2)", 0.3))},
            (("stepQ(0.9,-2)", 0), ("stepQ(0.9,3.9)", 2/3), ("stepQ(1,3)", 1),
             ("cruiseE(2,0.9,0.9)", 1), ("cruiseE(0.7,0.2,-0.1)", 0.7), ("cruiseE(0.5,0.2,0.2)", 0.5))),
    Fixture("dial-adjacency", "wisit-seo", WISIT, "src/lib/dialTimeline.ts",
            "turnDirection returns zero if either stage is not an in-range integer. Forward includes the last-to-first wrap. "
            "The first-to-last wrap is backward. Otherwise keep the existing order-based direction.",
            {"turnDirection": "if (![from,to].every(n => Number.isInteger(n) && n >= 0 && n < STAGE_COUNT) || from === to) return 0; "
             "if (from === 0 && to === STAGE_COUNT - 1) return -1; return to > from || to === nextStage(from) ? 1 : -1;"},
            {"turnDirection": (("turnDirection(0,4)", -1), ("turnDirection(-1,0)", 0))},
            (("turnDirection(4,0)", 1), ("turnDirection(3,2)", -1), ("turnDirection(1,3)", 1),
             ("turnDirection(2,2)", 0), ("turnDirection(1,2.5)", 0), ("nextStage(4)", 0), ("nearestBezel(60)", 90))),
    Fixture("dial-jump", "wisit-seo", WISIT, "src/lib/dialTimeline.ts",
            "turnDirection returns zero when either input is nonintegral. Normalize integer inputs modulo the stage count. "
            "Return zero for the same normalized stage, otherwise take the shorter direction around the ring. A tie goes forward.",
            {"turnDirection": "if (!Number.isInteger(from) || !Number.isInteger(to)) return 0; "
             "const forward = ((to - from) % STAGE_COUNT + STAGE_COUNT) % STAGE_COUNT; "
             "return forward === 0 ? 0 : forward <= STAGE_COUNT / 2 ? 1 : -1;"},
            {"turnDirection": (("turnDirection(0,3)", -1), ("turnDirection(1.5,2)", 0))},
            (("turnDirection(4,1)", 1), ("turnDirection(1,4)", -1), ("turnDirection(-1,0)", 1),
             ("turnDirection(1,6)", 0), ("turnDirection(0,-6)", -1), ("nextStage(4)", 0), ("normalizeAngle(-90)", 270))),
)


def show(repo, revision, path):
    return subprocess.check_output(["git", "-C", str(repo), "show", f"{revision}:{path}"], timeout=15).decode("utf-8")


def get_targets(room, fixture, source):
    path = room / fixture.path
    path.parent.mkdir(parents=True)
    path.write_bytes(source.encode("utf-8"))
    project.init_project(room)
    db = project.db_path(room)
    with closing(store.connect(db)) as conn:
        task = task_library.create_task(conn, fixture.name)["id"]
        evidence = transcript.record_message(conn, session_id="decision", role="user", content=fixture.decision)
        event = episodic.record_event(conn, repo=fixture.repo, type="decision", task_id=task, file=fixture.path,
                                      payload={"decision": fixture.decision, "status": "verified"},
                                      evidence=[{"type": "transcript", "message_id": evidence}])
    context = session_context.get_session_start_context(db, task_id=task, project_root=room, query=fixture.name)
    decision = next(item for item in context["items"] if item.get("event_id") == event)
    return {target["symbol"]: target for target in decision.get("code_targets", ())}


def run(args, scratch):
    js = Path(__file__).with_name("repository_js_grader.cjs")
    runner = ["node", str(js), str(args.dependencies.resolve(strict=True)), str(args.typescript.resolve(strict=True))]

    def grade(**data):
        result = subprocess.run(runner, input=json.dumps(data), capture_output=True, encoding="utf-8", timeout=10)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        return json.loads(result.stdout)

    assert grade(boundary=True) == {"contained": True}
    fixtures = []
    for fixture in CASES:
        repo = args.work / fixture.repo
        source = show(repo, fixture.revision, fixture.path)
        stages = None
        if fixture.repo == "wisit-seo":
            config = show(repo, fixture.revision, "src/config/hero.ts")
            stages = re.findall(r'key: "([a-z]+)"', config)
            assert stages == ["request", "ai", "assigned", "tracked", "completed"]
        checks = [*(row for rows in fixture.public.values() for row in rows), *fixture.hidden]
        assert not grade(source=source, heroStages=stages, replacements={}, checks=checks)["passed"]
        assert grade(source=source, heroStages=stages, replacements=fixture.reference, checks=checks)["passed"], fixture.name
        targets = get_targets(scratch / fixture.name, fixture, source)
        assert targets.keys() == fixture.reference.keys(), (fixture.name, targets)
        fixtures.append((fixture, source, stages, checks, targets))
    harness.emit("preflight", cases=len(fixtures), repositories=2, wasm_boundary=True)
    if args.regrade:
        rows = [json.loads(line) for line in args.regrade.read_text(encoding="utf-8-sig").splitlines()]
        scores = {(row["task"], row["arm"]): row for row in rows if row["type"] == "scored"}
        assert len(scores) == len(fixtures) * 2 and rows[-1]["type"] == "summary"
        for fixture, source, stages, checks, _ in fixtures:
            for arm in ("trace", "notes"):
                row = scores[fixture.name, arm]
                assert grade(source=source, heroStages=stages, replacements=row["replacements"], checks=checks)["passed"] == row["passed"]
        harness.emit("regrade", verdicts=len(scores), disagreements=0)
        return
    if args.diagnostic_preflight:
        fixture, source, stages, _, _ = fixtures[0]
        check, = fixture.public["seg"][:1]
        outcome = grade(source=source, heroStages=stages, replacements={}, checks=[check], diagnostics=True)
        assert outcome == {"passed": False, "check": "check 1", "expression": "seg(3,3,3)", "expected": 1, "observed": "null"}
        harness.emit("diagnostic_preflight", **outcome)
        return
    if args.preflight_only:
        return
    assert any(row["name"] == QWEN and row["digest"] == DIGEST for row in harness.request("/api/tags")["models"])
    harness.emit("environment", model=QWEN, digest=DIGEST, seed=66109, focused_source=args.focused_source,
                 max_calls_per_definition=2, exact_feedback=args.exact_feedback, inference_cost_usd=0)
    rng = Random(66109)
    scores = {arm: 0 for arm in ("trace", "notes")}
    for fixture, source, stages, checks, targets in fixtures:
        visible_source = source
        if args.focused_source:
            dependencies = ("clamp",) if fixture.repo == "rubaru-memory-pilot" else ()
            visible_source = grade(source=source, definitions=[*fixture.reference, *dependencies])["source"]
            if stages:
                visible_source = (f"const STAGE_COUNT = {len(stages)};\n"
                                  "const nextStage = (index: number) => (index + 1) % STAGE_COUNT;\n" + visible_source)
        for arm in rng.sample(tuple(scores), len(scores)):
            replacements = {}
            calls = tokens = 0
            start = perf_counter()
            for symbol, examples in fixture.public.items():
                text = targets[symbol]["decision_excerpt"] if arm == "trace" else fixture.decision
                prompt = (f"Edit only the body of {symbol} in {fixture.path}.\nApproved behavior:\n{text}\n\n"
                          f"Public examples:\n{json.dumps(examples)}\n\nCurrent source:\n{visible_source}\n\n"
                          "Return a JSON object with one body field containing only the statements inside the function braces. "
                          "Preserve the function's parameters and existing file scope.")
                for attempt in range(2):
                    calls += 1
                    request = {
                        "model": QWEN, "stream": False, "keep_alive": "5m",
                        "format": {"type": "object", "properties": {"body": {"type": "string"}}, "required": ["body"]},
                        "messages": [{"role": "user", "content": prompt}],
                        "options": {"temperature": 0, "seed": 66109, "num_ctx": 2048 if args.focused_source else 3072,
                                    "num_predict": 200 if args.focused_source else 240},
                    }
                    try:
                        response = harness.request("/api/chat", request)
                    except TimeoutError:
                        continue
                    tokens += response.get("prompt_eval_count") or 0
                    try:
                        body = json.loads(response["message"]["content"])["body"]
                    except (TypeError, ValueError, KeyError):
                        body = None
                    trial = {**replacements, symbol: body}
                    public_checks = ([*examples, *(row for prior in replacements for row in fixture.public[prior])]
                                     if args.exact_feedback else examples)
                    result = grade(source=source, heroStages=stages, replacements=trial, checks=public_checks,
                                   diagnostics=args.exact_feedback)
                    if response.get("done_reason") != "length" and result["passed"]:
                        replacements = trial
                        break
                    if attempt == 0:
                        feedback = json.dumps(result) if args.exact_feedback else result.get("check", "incomplete output")
                        prompt += f"\nPrevious proposal: {json.dumps(body)}\nPublic test result: {feedback}."
            result = grade(source=source, heroStages=stages, replacements=replacements, checks=checks)
            scores[arm] += result["passed"]
            harness.emit("scored", task=fixture.name, arm=arm, revision=fixture.revision, path=fixture.path,
                         calls=calls, input_tokens=tokens, elapsed_ms=round((perf_counter() - start) * 1000),
                         replacements=replacements, **result)
    harness.emit("summary", cases=len(fixtures), scores=scores)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--typescript", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--regrade", type=Path)
    parser.add_argument("--focused-source", action="store_true")
    parser.add_argument("--exact-feedback", action="store_true")
    parser.add_argument("--diagnostic-preflight", action="store_true")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run with assertions enabled")
    with TemporaryDirectory(prefix="trace-repo-js-", dir=args.work.resolve(strict=True)) as folder:
        run(args, Path(folder))
