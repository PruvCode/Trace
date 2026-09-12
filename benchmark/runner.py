"""Benchmark runner: the SOLE orchestrator (Phase 1).

Flow per run: load task+config -> ensure fixture -> isolated workspace ->
mock agent (timeout-guarded) -> mechanical evaluator -> JSONL result.
Every failure path still writes a schema-valid result line with success=false.
"""

from __future__ import annotations

import argparse
import datetime
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from agent.interface import AgentResult, Budget
from agent.mock_agent import MockAgent
from agent.tools import build_core_tools
from benchmark import evaluator as evaluator_mod
from benchmark import fairness as fairness_mod
from benchmark import loader as loader_mod
from benchmark import metrics as metrics_mod
from benchmark import results as results_mod
from benchmark import workspace as workspace_mod

PHASE1_AGENTS = ("mock",)


def _budget_from(config: dict) -> Budget:
    budget = config.get("budget", {})
    return Budget(
        max_turns=int(budget.get("max_turns", 10)),
        max_tool_calls=int(budget.get("max_tool_calls", 20)),
        timeout_seconds=float(budget.get("timeout_seconds", 300)),
    )


def _run_agent_guarded(agent, workspace, prompt, tools, budget: Budget):
    """Run agent with a wall-clock timeout; classify timeout/crash explicitly.

    Uses a detached daemon thread: on timeout the worker is abandoned (it may
    still be running when the evaluator executes -- a documented Phase 1
    limitation; the mock "slow" behavior performs no writes, so it cannot
    corrupt the evaluated tree).
    """
    box: dict = {}

    def target():
        try:
            box["result"] = agent.run(workspace, prompt, tools, budget)
        except Exception as exc:  # noqa: BLE001 - converted to error result
            box["raised"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout=budget.timeout_seconds)
    if worker.is_alive():
        return (
            AgentResult(
                status="timeout",
                termination_reason=(
                    f"agent exceeded budget.timeout_seconds="
                    f"{budget.timeout_seconds}"
                ),
                turns=0,
                tool_calls=0,
                tool_log=[],
                error="agent timeout",
            ),
            "timeout",
        )
    if "raised" in box:
        exc = box["raised"]
        return (
            AgentResult(
                status="error",
                termination_reason="agent raised",
                turns=0,
                tool_calls=0,
                tool_log=[],
                error=f"{type(exc).__name__}: {exc}",
            ),
            "raised",
        )
    result = box.get("result")
    if result is None:
        return (
            AgentResult(
                status="error",
                termination_reason="agent returned None",
                turns=0,
                tool_calls=0,
                tool_log=[],
                error="agent returned None",
            ),
            "raised",
        )
    return result, None


def run_single(
    task,
    config: dict,
    run_index: int,
    seed_base: int,
    work_root: Path,
    exp_id: str,
    results_path: Path,
    repo_root: Path,
    mock_behavior_override: str | None = None,
) -> dict:
    """Execute one isolated run; always appends exactly one result line."""
    timer = metrics_mod.RunTimer()
    configuration = config["name"]
    run_seed = f"{seed_base}-{run_index}"
    ws_name = f"ws_{task.task_id}_{configuration}_run{run_index}"
    dest = work_root / exp_id / ws_name
    agent_result = AgentResult(
        status="error",
        termination_reason="not_started",
        turns=0,
        tool_calls=0,
        tool_log=[],
        error=None,
    )
    eval_result = None
    workspace: Path | None = None
    run_error: str | None = None
    actual_head: str | None = None

    try:
        fixture_dir = (repo_root / task.fixture).resolve()
        actual_head = workspace_mod.ensure_fixture(fixture_dir)
        if actual_head != task.base_commit:
            raise RuntimeError(
                f"fixture HEAD {actual_head} != task base_commit {task.base_commit}"
            )
        workspace = workspace_mod.create_workspace(
            fixture_dir, task.base_commit, dest
        )
    except Exception as exc:  # noqa: BLE001 - recorded in result line
        run_error = f"workspace_setup_failed: {type(exc).__name__}: {exc}"

    if run_error is None:
        assert workspace is not None
        try:
            if config["agent"] not in PHASE1_AGENTS:
                raise RuntimeError(
                    f"Phase 1 supports only mock agents, got {config['agent']!r}"
                )
            behavior = mock_behavior_override or config.get("mock", {}).get(
                "behavior", "pass"
            )
            mock_cfg = config.get("mock", {})
            agent = MockAgent(
                behavior=behavior,
                sleep_seconds=float(mock_cfg.get("sleep_seconds", 30.0)),
            )
            prompt = task.prompt + (config.get("prompt_addendum") or "")
            budget = _budget_from(config)
            tools = build_core_tools(workspace)
            agent_result, agent_issue = _run_agent_guarded(
                agent, workspace, prompt, tools, budget
            )
            if agent_issue is not None:
                run_error = f"agent_{agent_issue}: {agent_result.error}"
        except Exception as exc:  # noqa: BLE001 - recorded in result line
            run_error = f"agent_failed: {type(exc).__name__}: {exc}"

    if workspace is not None and workspace.exists():
        try:
            eval_result = evaluator_mod.run_evaluator(
                workspace, task.eval_command, task.timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            run_error = (run_error + "; " if run_error else "") + (
                f"evaluator_failed: {type(exc).__name__}: {exc}"
            )

    try:
        git_status = (
            workspace_mod.workspace_git_status(workspace)
            if workspace is not None and workspace.exists()
            else []
        )
    except Exception:  # noqa: BLE001 - provenance best-effort only
        git_status = ["<status unavailable>"]

    latency = timer.elapsed()
    success = bool(eval_result is not None and eval_result.success)
    result = {
        "task_id": task.task_id,
        "configuration": configuration,
        "run": run_index,
        "base_commit": task.base_commit,
        "success": success,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "latency_seconds": round(latency, 3),
        "turns": agent_result.turns,
        "tool_calls": agent_result.tool_calls,
        # Provenance:
        "seed": run_seed,
        "model": config.get("model"),
        "agent": config.get("agent"),
        "task_version": task.task_version,
        "config_hash": config.get("config_hash"),
        "eval_command": task.eval_command,
        "exit_code": eval_result.exit_code if eval_result else None,
        "workspace": str(dest),
        "prompt_sha256": task.prompt_sha256,
        "fixture": task.fixture,
        "error": run_error or agent_result.error,
        "termination_reason": agent_result.termination_reason,
        "timed_out": eval_result.timed_out if eval_result else False,
        "tool_log": [asdict(r) for r in agent_result.tool_log],
        "git_status": git_status,
    }
    results_mod.append_result(results_path, result)
    return result


def discover_tasks(tasks_root: Path) -> list[Path]:
    return sorted(tasks_root.rglob("task.yaml"))


def run_experiment(
    task_refs: list[Path],
    config: dict,
    runs: int,
    seed_base: int,
    work_root: Path,
    exp_id: str,
    results_path: Path,
    repo_root: Path,
    mock_behavior_override: str | None = None,
) -> list[dict]:
    outcomes = []
    for task_ref in task_refs:
        task_dir = task_ref if task_ref.is_dir() else task_ref.parent
        task = loader_mod.load_task(task_dir, repo_root)
        for run_index in range(1, runs + 1):
            outcomes.append(
                run_single(
                    task,
                    config,
                    run_index,
                    seed_base,
                    work_root,
                    exp_id,
                    results_path,
                    repo_root,
                    mock_behavior_override,
                )
            )
    return outcomes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TRACE benchmark runner (Phase 1)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--task", type=Path, help="task directory containing task.yaml")
    group.add_argument("--all", dest="all_root", type=Path, help="run every task.yaml under this root")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--work-root", type=Path, default=Path("runs") / "workspaces")
    parser.add_argument("--exp-id", type=str, default=None)
    parser.add_argument("--runs-file", type=Path, default=None)
    parser.add_argument(
        "--mock-behavior",
        type=str,
        default=None,
        choices=["pass", "fail", "error", "slow"],
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    config = loader_mod.load_config(args.config)
    if args.task is not None:
        task_refs = [args.task / "task.yaml" if (args.task / "task.yaml").exists() else args.task]
    else:
        task_refs = discover_tasks(args.all_root)
        if not task_refs:
            print(f"no tasks found under {args.all_root}", file=sys.stderr)
            return 1
    exp_id = args.exp_id or (
        "exp_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")  # noqa: DTZ005 - local lab timestamps
    )
    work_root = args.work_root if args.work_root.is_absolute() else repo_root / args.work_root
    results_path = (
        args.runs_file
        if args.runs_file is not None
        else work_root / exp_id / "results.jsonl"
    )
    if args.runs_file is not None and not args.runs_file.is_absolute():
        results_path = repo_root / args.runs_file
    outcomes = run_experiment(
        task_refs,
        config,
        max(1, args.runs),
        args.seed,
        work_root,
        exp_id,
        results_path,
        repo_root,
        args.mock_behavior,
    )
    passed = sum(1 for o in outcomes if o["success"])
    print(f"runs={len(outcomes)} passed={passed} failed={len(outcomes) - passed}")
    print(f"results: {results_path}")
    for outcome in outcomes:
        print(
            f"  {outcome['task_id']} {outcome['configuration']} "
            f"run{outcome['run']}: success={outcome['success']} "
            f"turns={outcome['turns']} tools={outcome['tool_calls']} "
            f"latency={outcome['latency_seconds']}s"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
