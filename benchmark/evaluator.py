"""Mechanical evaluator: subprocess test execution, never agent self-report.

argv[0] == "python" is resolved to the current interpreter so task.yaml stays
portable across Windows/venv layouts. No shell.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from benchmark.schemas import EvalResult


def run_evaluator(
    workspace: Path, eval_command: list[str], timeout_seconds: int
) -> EvalResult:
    cmd = list(eval_command)
    if cmd[0] in ("python", "python3"):
        cmd[0] = sys.executable
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return EvalResult(
            success=False,
            exit_code=None,
            stdout=out[-4000:],
            stderr=(err + "\nEVALUATOR TIMEOUT").strip()[-4000:],
            timed_out=True,
        )
    return EvalResult(
        success=proc.returncode == 0,
        exit_code=proc.returncode,
        stdout=proc.stdout[-4000:],
        stderr=proc.stderr[-4000:],
        timed_out=False,
    )
