"""Exercise the offline auth-debug walkthrough in a disposable Git worktree."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

INVESTIGATION = "Timeout flows through get_session_timeout and refresh_token"
DECISION = "Keep the default at 30 minutes until the login rollout"


def run(root: Path, python: Path, source: Path) -> dict:
    root.mkdir(parents=True)
    example = root / "examples/auth-debug"
    shutil.copytree(source / "examples/auth-debug", example, ignore=shutil.ignore_patterns(".agent-memory", ".trace-service"))
    (root / ".gitignore").write_text(".agent-memory/\n.trace-service/\n", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=TRACE Smoke", "-c", "user.email=trace@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=missing-hooks", *args],
            cwd=root, check=True, capture_output=True, timeout=30,
        )

    def trace(*args):
        result = subprocess.run([str(python), "-I", "-m", "trace_memory.cli", *args], cwd=example,
                                capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout

    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-qm", "seed nested auth example")
    (root / "sibling.txt").write_text("sibling change\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "sibling only")

    trace("init")
    assert json.loads(trace("find", "get_session_timeout"))["file"] == "auth.py"
    assert json.loads(trace("callers", "get_session_timeout"))["caller"] == "refresh_token"
    trace("record", "--type", "investigation", "--symbol", "get_session_timeout", "--message", INVESTIGATION)
    saved = json.loads(trace("record", "--type", "decision", "--symbol", "get_session_timeout", "--message", DECISION))
    assert json.loads(trace("events", "--query", "rollout"))["id"] == saved["id"]
    context = json.loads(trace("context", "rollout"))
    assert context["kind"] == "event" and context["id"] == saved["id"] and context["content"] == DECISION
    history = json.loads(trace("history", "--path", "auth.py"))
    assert "outside this project" in history["subject"]
    assert {item["path"] for item in history["files"]} == {"README.md", "auth.py"}
    report = trace("report")
    assert "Git (facts" in report and "sibling only" not in report and DECISION in report
    return {"decision_id": saved["id"], "context_retrieved": True, "nested_git_report": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.scratch.resolve(), args.python.resolve(), Path(__file__).resolve().parents[2])))


if __name__ == "__main__":
    main()
