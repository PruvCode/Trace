"""Run matched edits to two files of pinned repositories with OpenAI Evals."""

from __future__ import annotations

import argparse
import ast
from contextlib import chdir, closing
from hashlib import sha256
import json
import os
from pathlib import Path
from random import Random
from secrets import token_hex
import subprocess
from tempfile import TemporaryDirectory
from textwrap import indent
from time import perf_counter
from unittest.mock import patch

from memory import episodic, session_context, store, synthetic, task_library, transcript
from research.phase5.grader import MAX_SOURCE, Grader
from research.preview import notes_trial as trial
from research.preview.fresh_multifile_cases import CASES, PINS
from research.preview.repository_js import show
from research.production.multi_file import QUALIFY, evaluate
from trace_memory import project

SEEDS = (52183, 52189)
ARMS = ("notes", "trace")
OUTPUT_TOKENS = 760
CONTEXT_TOKENS = 4096


def digest():
    paths = [Path(__file__), Path(__file__).with_name("fresh_multifile_cases.py")]
    return sha256(b"".join(path.read_bytes().replace(b"\r\n", b"\n") for path in paths)).hexdigest()


def definition(source, symbol):
    node = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == symbol)
    if ast.get_docstring(node):
        node.body.pop(0)
    return node, ast.unparse(node)


def sources_at_pin(repo, repo_name):
    prefix = f"src/{repo_name}/"
    raw = subprocess.check_output(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", PINS[repo_name], prefix],
                                  text=True)
    return {path: show(repo, PINS[repo_name], path) for path in raw.splitlines()
            if path.startswith(prefix) and path.endswith(".py")}


def file_paths(case):
    return tuple(f"src/{case.repo}/{module}.py" for module in case.paths)


def replacements(source, case, candidates):
    patched = dict(source)
    for path, symbol, change in zip(file_paths(case), case.symbols, candidates, strict=True):
        if change is None:
            continue
        before, original = definition(source[path], symbol)
        if trial.function_only(change, symbol, original) != change:
            raise ValueError(f"candidate must preserve the signature of {symbol}")
        lines = source[path].splitlines(keepends=True)
        lines[before.lineno - 1:before.end_lineno] = [change + "\n"]
        patched[path] = "".join(lines)
    return patched


def checks(grader, source, case, assertions):
    modules = {
        path.removeprefix("src/").removesuffix(".py").replace("/", ".").removesuffix(".__init__"):
        (path.endswith("/__init__.py"), code)
        for path, code in source.items()
    }
    marker = token_hex(12)
    script = f"""import sys, importlib.util
sources = {modules!r}
class Loader:
    def find_spec(self, name, path=None, target=None):
        if name in sources:
            return importlib.util.spec_from_loader(name, self, is_package=sources[name][0])
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        name = module.__name__
        exec(compile(sources[name][1], name, 'exec'), module.__dict__)
sys.meta_path.insert(0, Loader())
a = importlib.import_module({(case.repo + "." + case.paths[0])!r})
b = importlib.import_module({(case.repo + "." + case.paths[1])!r})
{trial.CHECK_HELPER}
def wraps_tag_error(filename):
    try:
        b.parse_wheel_filename(filename)
    except b.InvalidWheelFilename as exc:
        return isinstance(exc.__cause__, ValueError)
    return False
def wheel_parts(filename):
    name, version, build, tags = b.parse_wheel_filename(filename)
    return name, str(version), build, {{str(tag) for tag in tags}}
"""
    script += f"print({marker!r}, " + ", ".join(f"check(lambda: {expr})" for expr in assertions) + ")"
    ok, output = grader.run(script)
    verdict = output.decode(errors="replace").split()
    if not ok or verdict[:1] != [marker] or len(verdict) != len(assertions) + 1:
        return (False,) * len(assertions)
    return tuple(value == "True" for value in verdict[1:])


def context_for(room, source, case):
    room.mkdir()
    paths = file_paths(case)
    for path in paths:
        target = room / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source[path], encoding="utf-8")
    project.init_project(room)
    db = project.db_path(room)
    note = case.decision + "\nApproved checks:\n" + "\n".join(case.public)
    with closing(store.connect(db)) as conn:
        task = task_library.create_task(conn, case.name)["id"]
        retired = episodic.record_event(conn, type="decision", repo=case.repo, task_id=task,
                                        payload={"decision": "RETIRED_RULE", "status": "verified"})
        evidence = transcript.record_message(conn, session_id="approval", role="user", content=note)
        event = episodic.record_event(
            conn, type="decision", repo=case.repo, task_id=task,
            file=paths[0], symbol=case.symbols[0],
            payload={"decision": case.decision, "status": "verified", "acceptance": list(case.public), "supersedes": retired},
            evidence=[{"type": "transcript", "message_id": evidence}],
        )
        unrelated = task_library.create_task(conn, "unrelated")["id"]
        episodic.record_event(conn, type="decision", repo=case.repo, task_id=unrelated,
                              payload={"decision": "OTHER_TASK_RULE", "status": "verified"})
    result = session_context.get_session_start_context(
        db, task_id=task, project_root=room, query=" ".join((*case.symbols, *paths)),
        max_items=4, max_chars=3000,
    )
    selected = next(item for item in result["items"] if item.get("event_id") == event)
    block = synthetic.render_user_block(result["items"])
    if (selected["text"] != case.decision or not selected["evidence"]
            or tuple(selected.get("acceptance", ())) != case.public
            or "RETIRED_RULE" in block or "OTHER_TASK_RULE" in block):
        raise ValueError(f"memory preflight failed for {case.name}")
    return {"notes": note, "trace": block}


def prepare(grader, repos, root):
    fixtures = []
    for case in CASES:
        source = sources_at_pin(repos[case.repo], case.repo)
        originals = tuple(definition(source[path], symbol)[1]
                          for path, symbol in zip(file_paths(case), case.symbols, strict=True))
        first_body, second_body = case.reference
        second = originals[1]
        if second_body is None:
            original_call = "    tags = parse_tag(parts[-1])"
            if second.count(original_call) != 1:
                raise ValueError("wheel parser fixture drift")
            second = second.replace(original_call, "    try:\n    " + original_call
                                    + "\n    except ValueError as exc:\n        raise InvalidWheelFilename(str(exc)) from exc")
        else:
            second = second.splitlines()[0] + "\n" + indent(second_body, "    ")
        reference = (originals[0].splitlines()[0] + "\n" + indent(first_body, "    "), second)
        reference = tuple(trial.function_only(value, symbol, original)
                          for value, symbol, original in zip(reference, case.symbols, originals, strict=True))
        if any(value is None for value in reference):
            raise ValueError(f"invalid reference for {case.name}")
        accepted = case.public + case.hidden
        if all(checks(grader, source, case, accepted)) or not all(checks(grader, source, case, case.preserve)):
            raise ValueError(f"invalid unchanged baseline for {case.name}")
        if not all(checks(grader, replacements(source, case, reference), case, accepted + case.preserve)):
            raise ValueError(f"invalid reference for {case.name}")
        for partial in ((reference[0], None), (None, reference[1])):
            if all(checks(grader, replacements(source, case, partial), case, accepted)):
                raise ValueError(f"{case.name} does not need both files")
        contexts = context_for(root / case.name, source, case)
        trial.emit("fixture", task=case.name, paths=file_paths(case), preserved=len(case.preserve),
                   hidden=len(case.hidden), checked_both_files=True, recalled=len(case.public))
        fixtures.append((case, source, originals, contexts))
    for case in QUALIFY:
        if evaluate(grader, case.original, case) or not evaluate(grader, case.reference, case):
            raise ValueError(f"invalid qualification fixture {case.name}")
    return fixtures


def parse_reply(reply, symbols, originals):
    try:
        payload = json.loads(reply)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict) or set(payload) != set(symbols):
        return None
    sources = tuple(trial.function_only(payload[name], name, original)
                    if isinstance(payload[name], str) and len(payload[name]) <= MAX_SOURCE else None
                    for name, original in zip(symbols, originals, strict=True))
    return sources if all(sources) else None


def ask(model, seed, prompt, symbols, originals):
    start = perf_counter()
    reply = trial.request("/api/chat", {
        "model": model, "stream": False, "keep_alive": "5m",
        "messages": [{"role": "user", "content": prompt}],
        "format": {"type": "object", "properties": {name: {"type": "string"} for name in symbols},
                   "required": list(symbols), "additionalProperties": False},
        "options": {"num_ctx": CONTEXT_TOKENS, "num_predict": OUTPUT_TOKENS, "num_gpu": 0,
                    "seed": seed, "temperature": 0},
    }, timeout=420)
    text = reply["message"]["content"] if reply.get("done_reason") != "length" else None
    return (parse_reply(text, symbols, originals),
            [reply.get("prompt_eval_count") or 0, reply.get("eval_count") or 0, round((perf_counter()-start)*1000)])


def prompt_for(request, context, paths, symbols, originals):
    sources = "\n\n".join(f"{path}\n{original}" for path, original in zip(paths, originals, strict=True))
    return (f"{request}\n\nProject history:\n{context}\n\nSource:\n{sources}\n\n"
            f"Return JSON with exactly these keys: {', '.join(symbols)}. Each value must be the complete "
            "replacement function, with its original signature. Preserve behavior outside the approved change.")


def qualify_sample(grader, case, model):
    paths = ("protocol.py", "handler.py")
    names = tuple(next(node.name for node in ast.parse(code).body if isinstance(node, ast.FunctionDef))
                  for code in case.original)
    originals = tuple(definition(code, name)[1] for code, name in zip(case.original, names, strict=True))
    prompt = prompt_for(case.task, case.review, paths, names, originals)
    proposal, usage = ask(model, SEEDS[0], prompt, names, originals)
    passed = evaluate(grader, qualified_source(case, proposal), case)
    if not passed:
        proposal, more = ask(model, SEEDS[0], f"{prompt}\nThe draft failed. Recheck both outputs.", names, originals)
        usage = [left + right for left, right in zip(usage, more, strict=True)]
        passed = evaluate(grader, qualified_source(case, proposal), case)
    return dict(task=case.name, candidate=proposal, passed=passed, usage=usage)


def qualified_source(case, proposal):
    if not proposal:
        return None
    full = []
    for code, candidate in zip(case.original, proposal, strict=True):
        body = ast.parse(code).body
        original = next(node for node in body if isinstance(node, ast.FunctionDef))
        if trial.function_only(candidate, original.name, ast.unparse(original)) != candidate:
            return None
        imports = [ast.unparse(node) for node in body if isinstance(node, (ast.Import, ast.ImportFrom))]
        full.append("\n".join((*imports, candidate)))
    return tuple(full)


def score_sample(grader, sample, model):
    fixture, arm, seed = sample
    case, source, originals, contexts = fixture
    context = contexts[arm]
    prompt = prompt_for(case.request, context, file_paths(case), case.symbols, originals)
    proposal, usage = ask(model, seed, prompt, case.symbols, originals)
    draft = proposal
    tested = checks(grader, replacements(source, case, proposal), case, case.public) if proposal else ()
    attempts = 1
    if not tested or not all(tested):
        failed = [rule for rule, passed in zip(case.public, tested) if not passed]
        feedback = (f"Your draft: {json.dumps(draft)}\nFailed approved examples:\n" + "\n".join(failed)
                    if draft else "The first reply was not a valid pair of function definitions.")
        next_proposal, more = ask(model, seed, f"{prompt}\n\n{feedback}\nCorrect both functions.", case.symbols, originals)
        attempts += 1
        usage = [left + right for left, right in zip(usage, more, strict=True)]
        if next_proposal and sum(checks(grader, replacements(source, case, next_proposal), case, case.public)) >= sum(tested):
            proposal = next_proposal
    changed, regressed = score_patch(grader, fixture, proposal)
    return dict(task=case.name, arm=arm, seed=seed, candidate=proposal, passed=changed and not regressed,
                regression=regressed, attempts=attempts, usage=usage, context_sha256=sha256(context.encode()).hexdigest())


def score_patch(grader, fixture, proposal):
    case, source, *_ = fixture
    patch = replacements(source, case, proposal or (None, None))
    return all(checks(grader, patch, case, case.public + case.hidden)), not all(checks(grader, patch, case, case.preserve))


def run_evals(args, grader, fixtures):
    with patch.dict(os.environ, OPENAI_API_KEY="trace-local", OPENAI_BASE_URL="http://127.0.0.1:11434/v1"):
        from evals import Eval
        from evals.base import RunSpec
        from evals.record import LocalRecorder

    qualifying = args.mode == "qualify"
    name = f"trace_fresh_multifile.{args.mode}.v1"
    run_config = {"model": args.model, "model_digest": trial.MODELS[args.model], "protocol": digest(),
                  "pins": PINS, "seeds": [SEEDS[0]] if qualifying else SEEDS}
    samples = QUALIFY if qualifying else [
        (fixture, arm, seed) for seed in SEEDS for fixture in fixtures
        for arm in Random(f"{seed}:{fixture[0].name}").sample(ARMS, len(ARMS))
    ]

    class MatchedEval(Eval):
        def eval_sample(self, sample, rng):
            row = (qualify_sample(grader, sample, args.model) if qualifying
                   else score_sample(grader, sample, args.model))
            passed = row.pop("passed")
            recorder.record_match(passed, **row)
            recorder.flush_events()
            trial.emit("sample", passed=passed, **{key: value for key, value in row.items() if key != "candidate"})

        def run(self, recorder):
            self.eval_all_samples(recorder, samples)
            rows = [event.data for event in recorder.get_events("match")]
            report = {"passed": sum(row["correct"] for row in rows), "total": len(rows)} if qualifying else {
                arm: {"passed": sum(row["correct"] for row in rows if row["arm"] == arm),
                      "regressions": sum(row["regression"] for row in rows if row["arm"] == arm),
                      "per_seed": [sum(row["correct"] for row in rows if row["arm"] == arm and row["seed"] == seed)
                                   for seed in SEEDS]}
                for arm in ARMS
            }
            recorder.record_final_report(report)
            return report

    output = args.output.resolve()
    with output.open("xb"):
        pass
    with chdir(output.parent), patch.dict(os.environ, EVALS_SEQUENTIAL="1", EVALS_THREADS="1"):
        recorder = LocalRecorder(output.name, RunSpec(completion_fns=[args.model], eval_name=name,
                                 base_eval="trace_fresh_multifile", split=args.mode, run_config=run_config,
                                 created_by="trace-local"))
        trial.emit("summary", **MatchedEval([], Path(__file__).parent, name=name).run(recorder))


def matches(rows):
    return [row["data"] for row in rows if row.get("type") == "match"]


def verify_qualification(path, grader, model):
    rows = trial.read_rows(path)
    cfg = rows[0]["spec"]["run_config"]
    verdicts = {row["task"]: row for row in matches(rows)}
    if (cfg["model_digest"] != trial.MODELS[model] or cfg["protocol"] != digest()
            or rows[0]["spec"]["split"] != "qualify" or set(verdicts) != {case.name for case in QUALIFY}):
        raise ValueError("qualification receipt does not match this protocol and model")
    for case in QUALIFY:
        row = verdicts[case.name]
        if not row["correct"] or not evaluate(grader, qualified_source(case, row["candidate"]), case):
            raise ValueError(f"model failed two-file qualification: {case.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "qualify", "score", "regrade"))
    for repo in PINS:
        parser.add_argument(f"--{repo}", type=Path, required=True)
    parser.add_argument("--wasi", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", choices=trial.MODELS, default=trial.QWEN)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    if not __debug__ or (args.mode in {"qualify", "score"} and args.output is None):
        parser.error("enable assertions and provide --output for inference")
    if args.mode in {"score", "regrade"} and args.input is None:
        parser.error("--input requires the saved qualification (score) or score log (regrade)")
    grader = Grader(args.wasi, fuel=4_000_000_000)
    with TemporaryDirectory(prefix="trace-two-file-", dir=args.scratch) as directory:
        root = Path(directory)
        canary = root / "outside"
        canary.write_text("sandbox canary", encoding="utf-8")
        grader.verify_boundary(canary)
        fixtures = prepare(grader, {repo: getattr(args, repo) for repo in PINS}, root)
        if args.mode == "preflight":
            return
        if args.mode == "regrade":
            indexed = {fixture[0].name: fixture for fixture in fixtures}
            rows = matches(trial.read_rows(args.input))
            keys = {(row["task"], row["arm"], row["seed"]) for row in rows}
            if len(rows) != len(keys) or keys != {(name, arm, seed) for name in indexed for arm in ARMS for seed in SEEDS}:
                raise ValueError("incomplete or duplicate score matrix")
            for row in rows:
                changed, regressed = score_patch(grader, indexed[row["task"]], row["candidate"])
                if row["correct"] != (changed and not regressed) or row["regression"] != regressed:
                    raise ValueError(f"regrade disagreement for {row['task']}")
            trial.emit("regrade", verified=len(rows))
            return
        tags = {row["name"]: row["digest"] for row in trial.request("/api/tags")["models"]}
        if tags.get(args.model) != trial.MODELS[args.model]:
            raise ValueError("pinned local model is unavailable")
        if args.mode == "score":
            verify_qualification(args.input, grader, args.model)
        run_evals(args, grader, fixtures)


if __name__ == "__main__":
    main()
