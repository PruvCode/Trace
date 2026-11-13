"""Matched local edits on pinned TRACE functions with WASI grading."""

import argparse
import ast
from contextlib import closing
import json
from pathlib import Path
from random import Random
from secrets import token_hex
from tempfile import TemporaryDirectory
from time import perf_counter

from memory import episodic, session_context, store, task_library, transcript
from research.phase5.grader import Grader
from research.preview import effectiveness as harness
from research.preview.definition_trial import function_only
from research.preview.repository_js import show
from research.preview.trace2_evaluation import DIGEST, QWEN
from trace_memory import project

REVISION = "9e54673819caec3189d2c29a1ae26d5aea8a66a4"
CASES = (
    ("compact-text", "memory/session_context.py", "_truncate", "",
     "_truncate returns a prefix of length at most the limit for limits below four, treating negative limits as zero. "
     "At larger limits preserve the current head and tail split and separator. Its boolean reports whether text was shortened.",
     "def _truncate(text: str, limit: int) -> tuple[str, bool]:\n"
     "    if limit < 4:\n        return text[:max(0, limit)], len(text) > max(0, limit)\n"
     "    if len(text) <= limit:\n        return text, False\n"
     "    head = limit * 2 // 5\n    tail = limit - head - 3\n    return text[:head] + ' … ' + text[-tail:], True",
     (("_truncate('abcdef', 1)", ("a", True)), ("_truncate('', 0)", ("", False))),
     (("_truncate('abcdef', -1)", ("", True)), ("_truncate('abc', 3)", ("abc", False)),
      ("_truncate('abcdefghij', 6)", ("ab … j", True)), ("_truncate('abcdef', 6)", ("abcdef", False)))),
    ("page-limit", "memory/store.py", "clamp_limit", "DEFAULT_LIMIT = 10\nMAX_LIMIT = 50\n",
     "clamp_limit uses DEFAULT_LIMIT for booleans and values that cannot be converted to an integer, including infinity. "
     "For all other values preserve existing integer conversion and the clamp from one to MAX_LIMIT.",
     "def clamp_limit(limit: object) -> int:\n    if isinstance(limit, bool):\n        return DEFAULT_LIMIT\n"
     "    try:\n        value = int(limit)\n    except (TypeError, ValueError, OverflowError):\n        return DEFAULT_LIMIT\n"
     "    return max(1, min(MAX_LIMIT, value))",
     (("clamp_limit(True)", 10), ("clamp_limit(float('inf'))", 10)),
     (("clamp_limit(False)", 10), ("clamp_limit(float('-inf'))", 10), ("clamp_limit('75')", 50),
      ("clamp_limit(-3)", 1), ("clamp_limit('7')", 7), ("clamp_limit(None)", 10))),
)


def grade(grader, prelude, source, checks):
    if source is None:
        return False
    token = token_hex(16)
    script = [prelude, source]
    for call, expected in checks:
        script.extend((f"result = {call}", f"assert result == {expected!r} and type(result) is type({expected!r})"))
    script.append(f"print({token!r})")
    return grader.run("\n".join(script)) == (True, (token + "\n").encode())


def facts(room, path, symbol, source, decision):
    target = room / path
    target.parent.mkdir(parents=True)
    target.write_bytes(source.encode("utf-8"))
    project.init_project(room)
    db = project.db_path(room)
    with closing(store.connect(db)) as conn:
        task = task_library.create_task(conn, symbol)["id"]
        evidence = transcript.record_message(conn, session_id="decision", role="user", content=decision)
        event = episodic.record_event(conn, repo="TRACE", type="decision", task_id=task, file=path, symbol=symbol,
                                      payload={"decision": decision, "status": "verified"},
                                      evidence=[{"type": "transcript", "message_id": evidence}])
    context = session_context.get_session_start_context(db, task_id=task, project_root=room, query=symbol)
    selected = next(item for item in context["items"] if item.get("event_id") == event)
    target, = selected["code_targets"]
    assert target["path"] == path and target["symbol"] == symbol
    return target["decision_excerpt"]


def run(args, root):
    grader = Grader(args.wasi.resolve(strict=True))
    canary = root / "host.txt"
    canary.write_text("fixture outside WASI", encoding="utf-8")
    grader.verify_boundary(canary)
    fixtures = []
    for name, path, symbol, prelude, decision, reference, public, hidden in CASES:
        source = show(args.repo, REVISION, path)
        definition = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == symbol)
        original = ast.get_source_segment(source, definition)
        assert not grade(grader, prelude, original, public + hidden)
        assert grade(grader, prelude, reference, public + hidden)
        trace_facts = facts(root / name, path, symbol, source, decision)
        fixtures.append((name, path, symbol, prelude, original, decision, trace_facts, public, hidden))
    harness.emit("preflight", tasks=len(fixtures), repository="TRACE", revision=REVISION)
    if args.regrade:
        rows = [json.loads(line) for line in args.regrade.read_text(encoding="utf-8-sig").splitlines()]
        scores = {(row["task"], row["arm"]): row for row in rows if row["type"] == "scored"}
        assert len(scores) == len(fixtures) * 2 and rows[-1]["type"] == "summary"
        for name, _, _, prelude, _, _, _, public, hidden in fixtures:
            for arm in ("trace", "notes"):
                row = scores[name, arm]
                assert grade(grader, prelude, row["candidate"], public + hidden) == row["passed"]
        harness.emit("regrade", verdicts=len(scores), disagreements=0)
        return
    if args.preflight_only:
        return
    assert any(row["name"] == QWEN and row["digest"] == DIGEST for row in harness.request("/api/tags")["models"])
    rng = Random(20227)
    scores = {arm: 0 for arm in ("notes", "trace")}
    for name, path, symbol, prelude, original, decision, trace_facts, public, hidden in fixtures:
        for arm in rng.sample(tuple(scores), len(scores)):
            approved = trace_facts if arm == "trace" else decision
            examples = "\n".join(f"{call} -> {expected!r}" for call, expected in public)
            prompt = (f"Edit {symbol} in {path}. Preserve the signature.\nApproved behavior:\n{approved}\n\n"
                      f"Public examples:\n{examples}\n\nExisting constants and function:\n{prelude}{original}\n\n"
                      "Return only the updated function. Do not add imports or other definitions.")
            start = perf_counter()
            calls = tokens = 0
            candidate = original
            for attempt in range(2):
                calls += 1
                response = harness.request("/api/chat", {
                    "model": QWEN, "stream": False, "keep_alive": "5m",
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0, "seed": 20227, "num_ctx": 2048, "num_predict": 260},
                })
                tokens += response.get("prompt_eval_count") or 0
                proposal = function_only(response["message"]["content"], symbol, original)
                if response.get("done_reason") != "length" and grade(grader, prelude, proposal, public):
                    candidate = proposal
                    break
                if attempt == 0:
                    prompt += "\nThe previous proposal failed the public checks or did not provide one valid function."
            passed = grade(grader, prelude, candidate, public + hidden)
            scores[arm] += passed
            harness.emit("scored", task=name, arm=arm, path=path, passed=passed, candidate=candidate,
                         calls=calls, input_tokens=tokens, elapsed_ms=round((perf_counter() - start) * 1000))
    harness.emit("summary", cases=len(fixtures), scores=scores)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wasi", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--regrade", type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run with assertions enabled")
    with TemporaryDirectory(prefix="trace-repo-py-", dir=args.scratch.resolve(strict=True)) as directory:
        run(args, Path(directory))
