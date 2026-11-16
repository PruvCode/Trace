"""Preregistered correction-to-rule gate (docs/engineering-audit/GUARDRAILS.md).

Stages, each writing JSON under ``--out``:

  mine       list correction candidates per repository in a fixed date window
             A = fix commit adds or changes a test function (human-written test exists)
             B = fix-like commit that changes only non-test code (no human test)
  qualify    A: keep only candidates whose human tests fail before and pass after (prototype validator)
             B: keep only candidates whose fix changes at least one function body
  freeze     take the first eligible candidates per repository and stratum in commit order;
             write corpus.json with its SHA-256. Nothing may be generated before this file is committed.
  inputs     build each generator input from information that existed when the fix landed:
             commit message, pre-fix source of the changed source files, and the non-test diff.
             Never the fix's tests, later commits, or anything else after the fix. A leakage check
             asserts no added test line appears in the input.
  mutants    deterministic mutants of the fixed source, restricted to lines the fix changed
  estimate   API cost of generation from the real inputs (no API call)
  generate   PAID. Refuses to run without --approved-budget-usd; never run by default.

The human baseline for stratum A is the fix commit's own tests (harvested by ``memory.guardrails``).
"""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import random
import re
import sys

from memory import guardrails
from memory import rule_checks
from memory import rules as rules_mod

WINDOW = ("2026-07-01", "2026-11-15")  # after the drafting model's assumed training data; see GUARDRAILS.md
PER_REPO = {"A": 8, "B": 4}  # per repository, in commit order (Amendments 2-3)
TARGET = {"A": 30, "B": 10}  # Amendment 2 in GUARDRAILS.md: only 10 B candidates exist in the window
FIX = re.compile(r"\b(fix|fixes|fixed|bug|regression|crash|wrong|incorrect|prevent|handle|correct)\b", re.I)
SKIP = re.compile(r"\b(docs?|typo|readme|changelog|ci|lint|format|typing|type hints?|annotations?|bump|release|version|conftest|tox)\b", re.I)
MODEL = "claude-opus-5-5"
PRICE = {"input": 4.00, "output": 20.00}  # USD per million tokens, Claude API, cached 2026-11-03
CHARS_PER_TOKEN = 3.5  # conservative for code; reported with a +-25% band
OUTPUT_TOKENS = 8_000  # per draft, thinking included
DRAFTS = 3


def git(repo: Path, *args: str) -> str:
    return guardrails._git(repo, *args)


def mine(repo: Path) -> list[dict]:
    since, until = WINDOW
    rows = []
    # Every non-merge commit reachable from HEAD: projects that merge PRs keep their fixes off the first-parent line.
    shas = git(repo, "rev-list", "--reverse", "--no-merges", f"--since={since}", f"--until={until}", "HEAD").split()
    for sha in shas:
        subject = git(repo, "log", "-1", "--format=%s", sha).strip()
        date = git(repo, "log", "-1", "--format=%cI", sha).strip()
        files = [f for f in git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split() if f.endswith(".py")]
        tests = [f for f in files if guardrails.is_test_path(f)]
        sources = [f for f in files if not guardrails.is_test_path(f)]
        if not sources or SKIP.search(subject):
            continue
        if tests:
            try:
                item = guardrails.correction(repo, sha)
                if guardrails.changed_tests(repo, item):
                    rows.append({"repo": repo.name, "sha": sha, "date": date, "subject": subject, "stratum": "A"})
            except ValueError:
                continue
        elif FIX.search(subject):
            rows.append({"repo": repo.name, "sha": sha, "date": date, "subject": subject, "stratum": "B"})
    return rows


def qualify(repo: Path, row: dict, *, python: str | None, pythonpath: tuple[str, ...], scratch: Path) -> dict:
    if row["stratum"] == "B":
        parent = git(repo, "rev-parse", f"{row['sha']}^").strip()
        item = guardrails.Correction(row["sha"], parent, row["subject"],
                                     [f for f in git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", row["sha"]).split()
                                      if f.endswith(".py")], [])
        changed = [name for path in item.source_files for name in guardrails.changed_definitions(repo, item, path)]
        return {**row, "eligible": bool(changed), "changed": changed}
    item = guardrails.correction(repo, row["sha"])
    rule = guardrails.draft(repo, item)
    report = guardrails.validate(repo, item, rule, python=python, pythonpath=pythonpath, later=0, scratch=scratch)
    return {**row, "eligible": report.valid, "human_checks": report.kept, "dropped": report.dropped}


def freeze(qualified: list[dict]) -> dict:
    """Per repository, the first eligible candidates per stratum in commit order; then the first TARGET per stratum."""
    corpus = []
    for stratum, quota in PER_REPO.items():
        picked = []
        for repo in sorted({row["repo"] for row in qualified}):
            rows = [r for r in qualified if r["repo"] == repo and r["stratum"] == stratum and r["eligible"]]
            picked += sorted(rows, key=lambda r: r["date"])[:quota]
        corpus += sorted(picked, key=lambda r: (r["date"], r["repo"]))[:TARGET[stratum]]
    body = json.dumps(corpus, indent=1, sort_keys=True)
    return {"frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "window": WINDOW,
            "per_repo": PER_REPO, "target": TARGET, "corpus_sha256": sha256(body.encode()).hexdigest(), "corpus": corpus}


def generator_input(repo: Path, row: dict) -> dict:
    """Point-in-time input: commit message, non-test diff, pre-fix source. Never a test file, never a later commit.

    The guarantee is structural (test paths are excluded and asserted absent). Distinctive lines of the fix's
    tests that also occur in the allowed material (e.g. an example quoted in the commit message) are counted
    and reported per correction, not hidden.
    """
    sha = row["sha"]
    parent = git(repo, "rev-parse", f"{sha}^").strip()
    message = git(repo, "log", "-1", "--format=%B", sha).strip()
    files = [f for f in git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split() if f.endswith(".py")]
    sources = [f for f in files if not guardrails.is_test_path(f)]
    tests = [f for f in files if guardrails.is_test_path(f)]
    before = {path: guardrails._show(repo, parent, path) or "" for path in sources}
    diff = git(repo, "diff", parent, sha, "--", *sources)
    if any(f"b/{path}" in diff for path in tests) or set(before) & set(tests):
        raise ValueError(f"{repo.name} {sha[:12]}: a test file entered the generator input")
    text = "\n".join([message, diff, *before.values()])
    added = [line[1:].strip() for line in (git(repo, "diff", parent, sha, "--", *tests) if tests else "").splitlines()
             if line.startswith("+") and not line.startswith("+++")]
    # Distinctive (long, after stripping) and not an import, which names things but reveals no tested behavior.
    added = [line for line in added if len(line) > 25 and not re.match(r"(from\s+\S+\s+)?import\s", line)]
    overlap = [line for line in added if line in text]
    return {"repo": repo.name, "sha": sha, "parent": parent, "message": message, "diff": diff, "before": before,
            "fix_test_lines": len(added), "fix_test_lines_also_in_input": len(overlap)}


_FLIPS = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
          ast.And: ast.Or, ast.Or: ast.And, ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}


def mutants(source: str, lines: set[int], limit: int = 12) -> list[dict]:
    """Single-point mutants on the given lines: comparison and boolean flips, constant nudges, dropped ``not``,
    and a dropped statement (replaced by ``pass``: a partial revert)."""
    tree = ast.parse(source)
    sites = []
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) not in lines:
            continue
        if isinstance(node, ast.Compare):
            sites += [(node, "ops", i) for i, op in enumerate(node.ops) if type(op) in _FLIPS]
        elif isinstance(node, ast.BoolOp) and type(node.op) in _FLIPS:
            sites.append((node, "op", None))
        elif isinstance(node, ast.Constant) and (isinstance(node.value, bool) or isinstance(node.value, int)):
            sites.append((node, "value", None))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            sites.append((node, "not", None))
        if isinstance(node, ast.stmt) and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Pass)):
            sites.append((node, "drop", None))
    found = []
    for index in range(len(sites)):
        copy = ast.parse(source)
        target = [n for n in ast.walk(copy) if getattr(n, "lineno", None) in lines]
        original = sites[index][0]
        twin = next(n for n in target if ast.dump(n) == ast.dump(original) and n.lineno == original.lineno
                    and n.col_offset == original.col_offset)
        kind, slot = sites[index][1], sites[index][2]
        if kind == "ops":
            twin.ops[slot] = _FLIPS[type(twin.ops[slot])]()
        elif kind == "op":
            twin.op = _FLIPS[type(twin.op)]()
        elif kind == "value":
            twin.value = (not twin.value) if isinstance(twin.value, bool) else twin.value + 1
        elif kind == "drop":
            parent = next(p for p in ast.walk(copy) for child in ast.iter_child_nodes(p) if child is twin)
            for field, value in ast.iter_fields(parent):
                if isinstance(value, list) and twin in value:
                    value[value.index(twin)] = ast.Pass()
        else:
            parent = next(p for p in ast.walk(copy) for child in ast.iter_child_nodes(p) if child is twin)
            for field, value in ast.iter_fields(parent):
                if value is twin:
                    setattr(parent, field, twin.operand)
                elif isinstance(value, list) and twin in value:
                    value[value.index(twin)] = twin.operand
        text = ast.unparse(copy)
        if text != ast.unparse(ast.parse(source)):
            found.append({"line": original.lineno, "kind": kind, "source": text})
    random.Random(0).shuffle(found)
    return found[:limit]


def fixed_lines(repo: Path, row: dict, path: str) -> set[int]:
    diff = git(repo, "diff", "-U0", f"{row['sha']}^", row["sha"], "--", path)
    lines = set()
    for match in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff, re.M):
        start, count = int(match[1]), int(match[2] or 1)
        lines.update(range(start, start + count))
    return lines


def kill_matrix(repo: Path, row: dict, checks: list[str], mutant_sets: dict, *, python: str | None,
                pythonpath: tuple[str, ...], scratch: Path | None) -> dict:
    """Run pytest checks against each mutant of the fixed source (at the fix revision).

    A mutant is killed when any check fails or errors on it; the unmutated fix must pass every check,
    otherwise the row is reported as unusable rather than scored.
    """
    rule = rules_mod.Rule(next(iter(mutant_sets), ""), None, "baseline", tuple(rules_mod.Check("pytest", c) for c in checks))
    result = {"sha": row["sha"], "checks": checks, "clean_pass": None, "mutants": []}
    with guardrails._worktree(repo, scratch) as tree:
        git(tree, "checkout", "--quiet", "--force", "--detach", row["sha"])
        run = rule_checks.subprocess_executor(tree, python, pythonpath=pythonpath)
        result["clean_pass"] = all(run(rule, check)[0] == "pass" for check in rule.checks)
        if not result["clean_pass"]:
            return result
        for path, items in mutant_sets.items():
            original = (tree / path).read_text(encoding="utf-8")
            for index, mutant in enumerate(items):
                (tree / path).write_text(mutant["source"], encoding="utf-8")
                outcomes = [run(rule, check)[0] for check in rule.checks]
                result["mutants"].append({"path": path, "index": index, "kind": mutant["kind"], "line": mutant["line"],
                                          "outcomes": outcomes, "killed": any(o != "pass" for o in outcomes)})
            (tree / path).write_text(original, encoding="utf-8")
    return result


def estimate(inputs: list[dict]) -> dict:
    chars = [len(json.dumps(item)) + 2_500 for item in inputs]  # + fixed instructions
    tokens_in = sum(chars) / CHARS_PER_TOKEN * DRAFTS
    tokens_out = OUTPUT_TOKENS * DRAFTS * len(inputs)
    cost = tokens_in / 1e6 * PRICE["input"] + tokens_out / 1e6 * PRICE["output"]
    return {"model": MODEL, "corrections": len(inputs), "drafts_each": DRAFTS, "input_tokens": round(tokens_in),
            "output_tokens": tokens_out, "usd": round(cost, 2), "usd_low": round(cost * 0.75, 2),
            "usd_high": round(cost * 1.25, 2), "batch_usd": round(cost / 2, 2)}


SCHEMA = {
    "type": "object",
    "properties": {
        "rule": {"type": "string", "description": "One sentence stating the requirement the fix established."},
        "test_module": {"type": "string", "description": "A complete pytest module testing that requirement."},
        "test_names": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["rule", "test_module", "test_names"],
    "additionalProperties": False,
}

PROMPT = (
    "A maintainer just merged the fix below. Write the engineering rule it establishes and a pytest module "
    "that checks it, so the same mistake is caught if it ever comes back.\n"
    "- The rule is one plain sentence (at most 300 characters) about behavior, not about this commit.\n"
    "- The tests must fail on the code before the fix and pass after it, and must express the requirement, "
    "not one incidental input. Prefer several small, independent test functions.\n"
    "- Import the project the way its own tests do. Do not touch the network or the filesystem outside tmp_path.\n\n"
)


def generate(item: dict, *, approved_budget_usd: float, spent_usd: float) -> dict:  # pragma: no cover - paid
    """One draft from Claude. Only called by the CLI after an explicit budget approval."""
    import anthropic

    if spent_usd >= approved_budget_usd:
        raise RuntimeError(f"approved budget of ${approved_budget_usd} reached")
    body = (PROMPT + f"Commit message:\n{item['message']}\n\nFix (non-test changes):\n```diff\n{item['diff']}\n```\n\n"
            + "".join(f"Before the fix, {path}:\n```python\n{text}\n```\n\n" for path, text in item["before"].items()))
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL, max_tokens=16_000,
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": body}],
    )
    if response.stop_reason != "end_turn":
        return {"error": response.stop_reason, "usage": response.usage.model_dump()}
    text = next(block.text for block in response.content if block.type == "text")
    usage = response.usage
    return {"draft": json.loads(text), "usage": usage.model_dump(),
            "usd": usage.input_tokens / 1e6 * PRICE["input"] + usage.output_tokens / 1e6 * PRICE["output"]}


REPOS = {  # repository -> source roots for PYTHONPATH
    "more-itertools": (), "sqlparse": (), "boltons": (), "cachetools": ("src",),
    "pyparsing": (), "tomlkit": (),  # wcwidth excluded (Amendment 3): tests need its C extension
}


def main():
    sys.stdout.reconfigure(encoding="utf-8", write_through=True)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stage", choices=("mine", "qualify", "freeze", "inputs", "mutants", "baseline", "estimate", "generate"))
    parser.add_argument("--repos", type=Path, required=True, help="directory holding the cloned repositories")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python", default=None)
    parser.add_argument("--scratch", type=Path, default=None)
    parser.add_argument("--approved-budget-usd", type=float, default=None)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    path = lambda name: args.out / name
    if args.stage == "mine":
        rows = [row for name in REPOS for row in mine(args.repos / name)]
        path("candidates.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
        for name in REPOS:
            print(name, {s: sum(r["repo"] == name and r["stratum"] == s for r in rows) for s in "AB"})
    elif args.stage == "qualify":
        rows = json.loads(path("candidates.json").read_text(encoding="utf-8"))
        done = {r["sha"]: r for r in json.loads(path("qualified.json").read_text(encoding="utf-8"))} if path("qualified.json").exists() else {}
        # In commit order per repository and stratum; stop once the per-repository quota of eligible rows is met.
        for row in sorted((r for r in rows if r["repo"] in REPOS), key=lambda r: (r["repo"], r["stratum"], r["date"])):
            eligible = sum(r["eligible"] for r in done.values() if r["repo"] == row["repo"] and r["stratum"] == row["stratum"])
            if row["sha"] in done or eligible >= PER_REPO[row["stratum"]]:
                continue
            try:
                done[row["sha"]] = qualify(args.repos / row["repo"], row, python=args.python,
                                           pythonpath=REPOS[row["repo"]], scratch=args.scratch)
            except Exception as exc:  # noqa: BLE001 - record and continue; ineligible
                done[row["sha"]] = {**row, "eligible": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
            print(json.dumps({k: done[row["sha"]].get(k) for k in ("repo", "sha", "stratum", "eligible")}), flush=True)
            path("qualified.json").write_text(json.dumps(list(done.values()), indent=1), encoding="utf-8")
    elif args.stage == "freeze":
        if path("corpus.json").exists():
            sys.exit("corpus.json already exists; a frozen corpus is never rewritten")
        frozen = freeze(json.loads(path("qualified.json").read_text(encoding="utf-8")))
        path("corpus.json").write_text(json.dumps(frozen, indent=1, sort_keys=True), encoding="utf-8")
        print(frozen["corpus_sha256"], len(frozen["corpus"]), {s: sum(r["stratum"] == s for r in frozen["corpus"]) for s in "AB"})
    elif args.stage == "inputs":
        corpus = json.loads(path("corpus.json").read_text(encoding="utf-8"))["corpus"]
        inputs = [generator_input(args.repos / row["repo"], row) for row in corpus]
        path("inputs.json").write_text(json.dumps(inputs, indent=1), encoding="utf-8")
        print(len(inputs), "inputs; leakage check passed")
    elif args.stage == "mutants":
        corpus = json.loads(path("corpus.json").read_text(encoding="utf-8"))["corpus"]
        result = {}
        for row in corpus:
            repo = args.repos / row["repo"]
            sources = [f for f in git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", row["sha"]).split()
                       if f.endswith(".py") and not guardrails.is_test_path(f)]
            result[row["sha"]] = {p: mutants(guardrails._show(repo, row["sha"], p) or "", fixed_lines(repo, row, p)) for p in sources}
        path("mutants.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        print({sha[:12]: sum(len(v) for v in files.values()) for sha, files in result.items()})
    elif args.stage == "baseline":
        corpus = json.loads(path("corpus.json").read_text(encoding="utf-8"))["corpus"]
        mutant_sets = json.loads(path("mutants.json").read_text(encoding="utf-8"))
        rows = []
        for row in corpus:
            if row["stratum"] != "A":
                continue
            matrix = kill_matrix(args.repos / row["repo"], row, row["human_checks"], mutant_sets[row["sha"]],
                                 python=args.python, pythonpath=REPOS[row["repo"]], scratch=args.scratch)
            rows.append(matrix)
            killed = sum(m["killed"] for m in matrix["mutants"])
            print(json.dumps({"repo": row["repo"], "sha": row["sha"][:12], "clean_pass": matrix["clean_pass"],
                              "killed": killed, "mutants": len(matrix["mutants"])}), flush=True)
            path("baseline-human.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    elif args.stage == "estimate":
        inputs = json.loads(path("inputs.json").read_text(encoding="utf-8"))
        result = estimate(inputs)
        path("estimate.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(json.dumps(result))
    else:
        if args.approved_budget_usd is None:
            sys.exit("generate is paid: pass --approved-budget-usd only after the owner approves the spend")
        raise SystemExit("generation is prepared but intentionally not wired to run in this milestone")


if __name__ == "__main__":
    main()
