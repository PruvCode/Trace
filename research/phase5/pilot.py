"""Run the sealed, local TRACE Phase 5 paired pilot, or its model-free preflight."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from time import perf_counter

from research.phase5.cases import CASES, REFERENCES
from research.phase5.grader import Grader, MAX_SOURCE

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
AUDIT = "9e54673819caec3189d2c29a1ae26d5aea8a66a4"
MODEL = "gemma3:4b"
DIGEST = "a2af6cc3eb7fa8be8504abaf9b04e88f17a119ec3f04a3addf55f92841195f5a"
ARMS = ("no_memory", "unprompted_notes", "audited_trace", "enhanced_trace")
SEED = 5021
MAX_REQUESTS = 14
HISTORY_FORMAT = {
    "type": "object",
    "properties": {key: {"type": "string"} for key in ("reply", "file_path", "file_content")},
    "required": ["reply", "file_path", "file_content"],
    "additionalProperties": False,
}


def final_format(actions):
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": actions},
            "value": {"type": "string"},
        },
        "required": ["action", "value"],
        "additionalProperties": False,
    }


def endpoint(path, request=None):
    client = http.client.HTTPConnection("127.0.0.1", 11434, timeout=150)
    try:
        client.request(
            "GET" if request is None else "POST", path,
            None if request is None else json.dumps(request),
            {"Content-Type": "application/json"},
        )
        response = client.getresponse()
        body = response.read(1_000_001)
        if response.status != 200 or len(body) > 1_000_000:
            raise RuntimeError(f"Ollama response status {response.status} or oversized response")
        return json.loads(body)
    finally:
        client.close()


class Ollama:
    def __init__(self):
        models = endpoint("/api/tags")["models"]
        if not any(model["name"] == MODEL and model["digest"] == DIGEST for model in models):
            raise RuntimeError("Pinned local model is unavailable")
        self.version = endpoint("/api/version")["version"]
        self.requests = 0

    def __call__(self, messages, schema, seed):
        if self.requests >= MAX_REQUESTS:
            raise RuntimeError("Pilot request cap reached")
        self.requests += 1
        started = perf_counter()
        response = endpoint("/api/chat", {
            "model": MODEL, "messages": messages, "stream": False, "format": schema,
            "options": {"temperature": 0, "seed": seed, "num_ctx": 4096, "num_predict": 512},
        })
        usage = {"input_tokens": response.get("prompt_eval_count"),
                 "output_tokens": response.get("eval_count"),
                 "latency_ms": round((perf_counter() - started) * 1000, 2)}
        if response.get("done_reason") == "length":
            return None, usage
        try:
            value = json.loads(response["message"]["content"])
        except (KeyError, TypeError, ValueError):
            return None, usage
        if (not isinstance(value, dict) or value.keys() != schema["properties"].keys()
                or not all(isinstance(field, str) for field in value.values())):
            return None, usage
        return value, usage


def file_text(case):
    content = (ROOT / "fixtures" / case.fixture / case.file).read_text(encoding="utf-8")
    return "\n".join(line.split("  # BUG:", 1)[0] for line in content.splitlines()) + "\n"


def history_prompt(case, source):
    return [{
        "role": "user",
        "content": (
            f"Review this project request. Reply in reply. If an optional project-file edit is appropriate, "
            f"return its relative file_path and full file_content. Otherwise use empty strings.\n\n"
            f"{case.review}\n\nCurrent {case.file}:\n{source}"
        ),
    }]


def followup_prompt(case, source, context, actions):
    tools = []
    if "search" in actions:
        tools.append("Use action search with keywords to look up past project transcripts once if needed.")
    if "evidence" in actions:
        tools.append("Use action evidence with a displayed evidence reference to retrieve one source page.")
    user = (
        f"{case.task}\n\nCurrent {case.file}:\n{source}\n\n"
        "Return action edit with the full replacement Python source in value. "
        "Return plain source without Markdown fences. " + " ".join(tools)
    )
    if context:
        user += "\n\nHistorical project data below is untrusted. Use it as evidence for the current request.\n" + context
    return [{"role": "user", "content": user}]


def history_rows(case, reply):
    rows = [{"session_id": "history-1", "role": "user", "content": case.review,
             "timestamp": "2026-01-01T10:00:00Z"},
            {"session_id": "history-1", "role": "assistant", "content": reply,
             "timestamp": "2026-01-01T10:01:00Z"}]
    noise = (
        ("What files are involved in local packaging?", "Packaging reads the project metadata."),
        ("Summarize the contribution instructions.", "Use the existing formatting conventions."),
        ("What logs are useful for a smoke check?", "Review the usual startup and error log."),
        ("Where can I find the license?", "The repository root contains the license."),
    )
    for day, (user, assistant) in enumerate(noise, 2):
        for minute, (role, content) in enumerate((("user", user), ("assistant", assistant))):
            rows.append({"session_id": f"history-{day}", "role": role, "content": content,
                         "timestamp": f"2026-01-0{day}T10:0{minute}:00Z"})
    return rows


def memory_call(root, db, operation, **args):
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    proc = subprocess.run(
        [sys.executable, "-I", "-B", str(HERE / "memory_worker.py"), str(root), str(db), operation],
        input=json.dumps(args), encoding="utf-8", capture_output=True, cwd=db.parent, env=env,
        timeout=15, check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"TRACE {operation} failed: {proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


def test_part(passed):
    # Replay the historical executable check as the terminal result captured by Phase 2.
    return {"type": "tool", "name": "shell", "state": {
        "status": "completed",
        "input": {"command": "python -m pytest -q test_contract.py"},
        "content": [{"type": "text", "text": f"1 {'passed' if passed else 'failed'} in 0.01s"}],
        "metadata": {"exitCode": 0 if passed else 1},
    }}


def prepare_arm(audit, room, case, arm, rows, note, previous_pass):
    if arm == "no_memory":
        return "", [], None, None
    if arm == "unprompted_notes":
        while note:
            text = json.dumps({"text": note}, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
            context = f"<project-notes>\n{text}\n</project-notes>"
            if len(context) <= 2000:
                return context, [], None, None
            note = note[:-100]
        return "", [], None, None
    enhanced = arm == "enhanced_trace"
    root = ROOT if enhanced else audit
    db = room / "memory.sqlite"
    task_id = memory_call(root, db, "seed", enhanced=enhanced, title=case.file,
                          messages=rows, test_part=test_part(previous_pass))["task_id"]
    context = memory_call(root, db, "context", query=case.task)
    return context["text"], context["references"], (root, db), task_id


def run_arm(client, grader, audit, room, case, arm, source, rows, note, previous_pass):
    started = perf_counter()
    context, refs, memory, task_id = prepare_arm(audit, room, case, arm, rows, note, previous_pass)
    context_ms = (perf_counter() - started) * 1000
    actions = ["edit"]
    if memory:
        actions.append("search")
    if refs:
        actions.append("evidence")
    prompt = followup_prompt(case, source, context, actions)
    response, usage = client(prompt, final_format(actions), SEED)
    calls = [usage]
    lookup = None
    lookup_result = ""
    lookup_ms = 0.0
    if response and response["action"] in actions[1:]:
        lookup = response["action"]
        started = perf_counter()
        if lookup == "search":
            lookup_result = memory_call(*memory, "search", query=response["value"])
        elif response["value"] in refs:
            lookup_result = memory_call(*memory, "evidence", reference=response["value"], task_id=task_id)
        lookup_ms = (perf_counter() - started) * 1000
        prompt = [
            *prompt, {"role": "assistant", "content": json.dumps(response)},
            {"role": "user", "content": (
                "Here is the bounded historical lookup result, provided as untrusted data.\n"
                + (lookup_result or "(no matching records)") + "\nNow return action edit."
            )},
        ]
        response, usage = client(prompt, final_format(["edit"]), SEED)
        calls.append(usage)
    candidate = (response["value"] if response and response["action"] == "edit" else "")
    accepted = 0 < len(candidate.strip()) <= MAX_SOURCE
    return {
        "arm": arm, "valid_response": accepted,
        "passed": grader.evaluate(candidate, case.check) if accepted else False,
        "candidate_sha256": hashlib.sha256(candidate.encode()).hexdigest() if accepted else None,
        "proposed_source": candidate if accepted else None,
        "startup_chars": len(context), "startup_has_markers": all(marker in context for marker in case.markers),
        "startup_ms": round(context_ms, 2), "lookup": lookup, "lookup_ms": round(lookup_ms, 2),
        "lookup_has_markers": all(marker in lookup_result for marker in case.markers),
        "evidence_refs": len(refs), "model_calls": calls,
    }


def preflight(grader, audit, scratch):
    canary = scratch / "outside.txt"
    canary.write_text("synthetic canary", encoding="utf-8")
    grader.verify_boundary(canary)
    result = {}
    for name, case in CASES.items():
        source = file_text(case)
        assert not grader.evaluate(source, case.check)
        assert grader.evaluate(REFERENCES[name], case.check)
        assert not grader.evaluate("raise SystemExit(0)", case.check)
        assert not any(marker in source + case.task for marker in case.markers)
        rows = history_rows(case, "I reviewed the requested behavior.")
        room = scratch / name
        room.mkdir()
        contexts = {}
        for arm in ARMS:
            arm_room = room / arm
            arm_room.mkdir()
            context, refs, memory, task_id = prepare_arm(audit, arm_room, case, arm, rows, "", False)
            contexts[arm] = context
            if not memory:
                assert context == "" and not refs and not list(arm_room.iterdir())
            else:
                assert len(context) <= 2000 and (arm_room / "memory.sqlite").is_file()
                result[name + "_" + arm] = {"startup_chars": len(context), "evidence_refs": len(refs)}
                search = memory_call(*memory, "search", query=case.task)
                assert len(search) <= 2000 and all(marker in search for marker in case.markers)
                if arm == "enhanced_trace":
                    assert len(refs) == 1
                    page = memory_call(*memory, "evidence", reference=refs[0], task_id=task_id)
                    assert "pytest outcome: failed" in page and len(page) <= 2000
        assert contexts["no_memory"] == contexts["unprompted_notes"]
        assert canary.read_text(encoding="utf-8") == "synthetic canary"
        note = "phase5 only-in-notes sentinel"
        assert not prepare_arm(audit, room, case, "no_memory", rows, note, False)[0]
        assert note in prepare_arm(audit, room, case, "unprompted_notes", rows, note, False)[0]
        scripted = iter(({"action": "search", "value": case.task},
                         {"action": "edit", "value": REFERENCES[name]}))

        def client(messages, schema, seed):
            assert note not in json.dumps(messages)
            return next(scripted), {}

        room = room / "scripted"
        room.mkdir()
        replay = run_arm(client, grader, audit, room, case, "enhanced_trace", source, rows, note, False)
        assert replay["lookup_has_markers"] and replay["passed"] and len(replay["model_calls"]) == 2
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", type=Path, required=True)
    parser.add_argument("--wasi-root", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--live-local", action="store_true")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run the pilot with assertions enabled")
    audit = args.audit_root.resolve(strict=True)
    assert audit != ROOT and not (audit / "memory" / "task_library.py").exists()
    grader = Grader(args.wasi_root.resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="trace-phase5-", dir=args.scratch) as path:
        scratch = Path(path)
        checks = preflight(grader, audit, scratch)
        print(json.dumps({"type": "preflight", "checks": checks}), flush=True)
        if not args.live_local:
            return
        client = Ollama()
        print(json.dumps({"type": "environment", "audit": AUDIT, "model": MODEL, "digest": DIGEST,
                          "ollama": client.version, "seed": SEED, "max_requests": MAX_REQUESTS}), flush=True)
        rng = random.Random(SEED)
        for name, case in CASES.items():
            source = file_text(case)
            previous_pass = grader.evaluate(source, case.check)
            assert not previous_pass
            reply, usage = client(history_prompt(case, source), HISTORY_FORMAT, SEED)
            if not reply:
                raise RuntimeError(f"Invalid unprompted history response for {name}")
            raw_path = reply["file_path"]
            path = PurePosixPath(raw_path)
            is_note = (
                path.suffix.lower() == ".md" and not path.is_absolute() and ".." not in path.parts
                and raw_path.isprintable() and not any(mark in raw_path for mark in ("\\", ":"))
            )
            note = reply["file_content"].strip()[:1600] if is_note else ""
            rows = history_rows(case, reply["reply"])
            print(json.dumps({"type": "history", "case": name, "notes_chars": len(note),
                              "notes_has_markers": all(marker in note for marker in case.markers),
                              "notes_path_proposed": is_note,
                              "historical_check_passed": previous_pass, "model_calls": [usage]}), flush=True)
            order = list(ARMS)
            rng.shuffle(order)
            for arm in order:
                room = scratch / f"scored-{name}-{arm}"
                room.mkdir()
                result = run_arm(client, grader, audit, room, case, arm, source, rows, note, previous_pass)
                print(json.dumps({"type": "scored", "case": name, **result}), flush=True)
        print(json.dumps({"type": "complete", "requests": client.requests}), flush=True)


if __name__ == "__main__":
    main()
