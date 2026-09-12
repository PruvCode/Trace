"""Minimal pilot entry point (Phase 5.1 skeleton).

Runs a task × configuration matrix by delegating to the existing benchmark
runner. This is orchestration only: no evaluation, aggregation, statistics,
or interpretation logic lives here (that belongs to later phases, if at all).

All output is labelled PILOT so it can never be mistaken for final evidence.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from benchmark.runner import main as run_benchmark

PILOT_BANNER = "PILOT — calibration run only, not benchmark evidence"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=PILOT_BANNER)
    parser.add_argument("--tasks-root", type=Path, default=Path("tasks"))
    parser.add_argument(
        "--config",
        dest="configs",
        type=Path,
        action="append",
        default=[],
        help="repeatable; defaults to baseline + reference_memory",
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--work-root", type=Path, default=Path("runs") / "workspaces")
    parser.add_argument("--exp-id", type=str, default=None)
    parser.add_argument("--runs-file", type=Path, default=None)
    parser.add_argument("--mock-behavior", type=str, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configs = list(args.configs) or [
        Path("configs/baseline.yaml"),
        Path("configs/reference_memory.yaml"),
    ]
    print(PILOT_BANNER)
    failures = 0
    for config in configs:
        print(f"PILOT run: tasks={args.tasks_root} config={config}")
        cmd = [
            "--all",
            str(args.tasks_root),
            "--config",
            str(config),
            "--runs",
            str(args.runs),
            "--seed",
            str(args.seed),
            "--work-root",
            str(args.work_root),
        ]
        if args.exp_id is not None:
            cmd += ["--exp-id", args.exp_id]
        if args.runs_file is not None:
            cmd += ["--runs-file", str(args.runs_file)]
        if args.mock_behavior is not None:
            cmd += ["--mock-behavior", args.mock_behavior]
        code = run_benchmark(cmd)
        print(f"PILOT done: config={config} exit={code}")
        failures += code != 0
    print(f"{PILOT_BANNER}: {len(configs) - failures}/{len(configs)} configs ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
