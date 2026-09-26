"""Benchmark runner: the SOLE orchestrator (Phase 4).

Flow per run: load task+config -> ensure fixture -> isolated workspace ->
memory backend setup -> agent (timeout-guarded) -> mechanical evaluator ->
JSONL result. Every failure path still writes a schema-valid result line
with success=false. Memory setup failure never falls back to baseline.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import random
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from agent.interface import AgentResult, Budget
from agent.llm_agent import (
    LLMAgent,
    LLMAuthError,
    LLMProviderError,
    OpenAICompatibleClient,
)
from agent.mock_agent import MockAgent
from agent.opencode_agent import (
    CONFIG_FILENAME as OPENCODE_CONFIG_FILENAME,
)
from agent.opencode_agent import (
    OpenCodeAgent,
)
from agent.opencode_agent import (
    build_opencode_config as build_opencode_base_config,
)
from agent.tools import build_core_tools
from benchmark import evaluator as evaluator_mod
from benchmark import fairness as fairness_mod
from benchmark import loader as loader_mod
from benchmark import metrics as metrics_mod
from benchmark import results as results_mod
from benchmark import workspace as workspace_mod
from memory.baseline import NullBackend
from memory.reference import ReferenceBackend


def _create_backend(config: dict, repo_root: Path, task_preseed=()):
    """Explicit backend selection. Unknown names fail loudly (never default)."""
    name = config.get("backend")
    if name is None or name == "null":
        return NullBackend()
    if name == "reference":
        memory_cfg = config.get("memory") or {}
        merged = list(task_preseed or []) + list(memory_cfg.get("preseed", ()))
        return ReferenceBackend(repo_root=repo_root, preseed=merged)
    raise RuntimeError(f"unknown_backend: {name!r}")


def _create_agent(config: dict, mock_behavior_override: str | None = None):
    """Explicit agent selection. Unknown names fail loudly (never default)."""
    kind = config.get("agent")
    if kind == "mock":
        behavior = mock_behavior_override or config.get("mock", {}).get(
            "behavior", "pass"
        )
        mock_cfg = config.get("mock", {})
        return MockAgent(
            behavior=behavior,
            sleep_seconds=float(mock_cfg.get("sleep_seconds", 30.0)),
        )
    if kind == "llm":
        params = config.get("model_parameters") or {}
        try:
            client = OpenAICompatibleClient(
                model=config.get("model"),
                base_url=params.get("base_url"),
                request_timeout=float(params.get("request_timeout", 60.0)),
            )
        except LLMAuthError:
            raise
        return LLMAgent(client, model=config.get("model"))
    if kind == "opencode":
        return OpenCodeAgent(model=config.get("model"))
    raise RuntimeError(f"unknown_agent: {kind!r}")


def build_opencode_config_with_memory(mcp_command: list[str]) -> dict:
    """Runner-owned seam: local trace_memory server on the minimal base config.

    Baseline never calls this (no ``mcp`` key). Reference calls it with the
    backend's MCPConfig.command so the out-of-process OpenCode binary spawns
    the same per-run database. Neither arm writes a `permission` block:
    free-tier `opencode run` rejects custom permission blocks with 403
    FreeTierError. Lives in the runner (not the agent) to keep
    memory/MCP details out of the agent layer; runner.py already holds the
    narrow ``mcp`` architecture exemption for this purpose.
    """
    base = build_opencode_base_config()
    base["mcp"] = {
        "trace_memory": {
            "type": "local",
            "enabled": True,
            "command": list(mcp_command),
        }
    }
    return base


def write_opencode_config_for_run(
    workspace: Path, mcp_command: list[str] | tuple | None
) -> None:
    """Write the per-run opencode.json when a memory server exists.

    No-op when ``mcp_command`` is empty (baseline): the agent writes its
    minimal config itself. The file lives inside the isolated
    per-run workspace and the agent removes it afterwards.
    """
    if not mcp_command:
        return
    cfg = build_opencode_config_with_memory(list(mcp_command))
    (workspace.resolve() / OPENCODE_CONFIG_FILENAME).write_text(
        json.dumps(cfg, indent=2), encoding="utf-8"
    )


def _benchmark_version() -> str:
    """Package version for provenance; never fails the run."""
    try:
        from importlib.metadata import version

        return version("trace")
    except Exception:  # noqa: BLE001 - provenance best-effort only
        return "unknown"


def _utc_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


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
    backend = None
    mcp_config = None
    mcp_tool_names: list[str] = []

    try:
        fixture_dir = (repo_root / task.fixture).resolve()
        # Seeders for generated fixtures return their (single) HEAD, which is
        # the task's base commit. The SWE-bench fixture is a shared historical
        # clone, so it is told which commit this task needs and verifies that
        # one is present locally instead of assuming HEAD.
        actual_head = workspace_mod.ensure_fixture(
            fixture_dir, base_commit=task.base_commit
        )
        if actual_head != task.base_commit:
            raise RuntimeError(
                f"fixture HEAD {actual_head} != task base_commit {task.base_commit}"
            )
        workspace = workspace_mod.create_workspace(
            fixture_dir, task.base_commit, dest
        )
    except Exception as exc:  # noqa: BLE001 - recorded in result line
        run_error = f"workspace_setup_failed: {type(exc).__name__}: {exc}"

    try:
        if run_error is None:
            assert workspace is not None
            try:
                backend = _create_backend(config, repo_root, task.preseed)
                agent = _create_agent(config, mock_behavior_override)
            except LLMAuthError as exc:
                run_error = f"llm_auth_missing: {exc}"
            except LLMProviderError as exc:
                run_error = f"llm_provider_error: {exc}"
            except RuntimeError as exc:
                message = str(exc)
                if message.startswith(("unknown_backend:", "unknown_agent:")):
                    run_error = message
                else:
                    run_error = f"agent_failed: {type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - recorded in result line
                run_error = f"agent_failed: {type(exc).__name__}: {exc}"
        if run_error is None:
            assert backend is not None
            try:
                mcp_config = backend.setup(workspace)
            except Exception as exc:  # noqa: BLE001 - setup phase is memory's
                run_error = f"memory_setup_failed: {type(exc).__name__}: {exc}"
        if run_error is None:
            # OpenCode reference seam (benchmark-owned): point the
            # out-of-process binary at this run's database via opencode.json.
            # Baseline (empty command) writes nothing, so it gets no memory.
            try:
                if config.get("agent") == "opencode" and getattr(
                    mcp_config, "command", None
                ):
                    assert workspace is not None
                    write_opencode_config_for_run(
                        workspace, list(mcp_config.command)
                    )
            except Exception as exc:  # noqa: BLE001 - recorded in result line
                run_error = f"agent_failed: {type(exc).__name__}: {exc}"
        if run_error is None:
            try:
                mcp_tools = backend.tool_definitions()
                mcp_tool_names = [t.name for t in mcp_tools]
                prompt = task.prompt + (config.get("prompt_addendum") or "")
                budget = _budget_from(config)
                tools = build_core_tools(workspace) + mcp_tools
                agent_result, agent_issue = _run_agent_guarded(
                    agent, workspace, prompt, tools, budget
                )
                if agent_issue == "timeout":
                    run_error = f"agent_timeout: {agent_result.error}"
                elif agent_issue is not None:
                    # In-thread agent crashes arrive as text; provider errors
                    # keep their class prefix for taxonomy mapping.
                    message = agent_result.error or ""
                    if message.startswith("LLMAuthError:"):
                        run_error = f"llm_auth_missing: {message}"
                    elif message.startswith("LLMProviderError:"):
                        run_error = f"llm_provider_error: {message}"
                    else:
                        run_error = f"agent_failed: {message}"
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
    finally:
        # Memory teardown never masks the run outcome.
        if backend is not None:
            try:
                backend.teardown()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass

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
    in_tok = agent_result.input_tokens
    out_tok = agent_result.output_tokens
    # Total requires exact provider usage for BOTH directions; otherwise None
    # (never estimated). token_source marks which case this run is.
    if in_tok is not None and out_tok is not None:
        total_tok: int | None = in_tok + out_tok
        token_source = "provider"
    else:
        total_tok = None
        token_source = "unknown"
    memory_names = set(mcp_tool_names)
    memory_tool_calls = sum(
        1 for record in agent_result.tool_log if record.name in memory_names
    )
    result = {
        "task_id": task.task_id,
        "configuration": configuration,
        "run": run_index,
        "base_commit": task.base_commit,
        "success": success,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": total_tok,
        "latency_seconds": round(latency, 3),
        "turns": agent_result.turns,
        "tool_calls": agent_result.tool_calls,
        "memory_tool_calls": memory_tool_calls,
        "token_source": token_source,
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
        "timestamp": _utc_timestamp(),
        "benchmark_version": _benchmark_version(),
    }
    results_mod.append_result(results_path, result)
    return result


def discover_tasks(tasks_root: Path) -> list[Path]:
    """Runnable corpus discovery: skips scaffold dirs (underscore-prefixed).

    tasks/_template/ documents the schema but pins a placeholder commit, so
    it must never enter a pilot matrix as a bogus error row.
    """
    found = []
    for path in sorted(tasks_root.rglob("task.yaml")):
        rel_parts = path.parent.relative_to(tasks_root).parts
        if any(part.startswith("_") for part in rel_parts):
            continue
        found.append(path)
    return found


def _execution_order(task_refs: list[Path], runs: int, shuffle_order=None):
    """Deterministic (task, run) execution sequence.

    Default None preserves task-major order. An int seed shuffles the full
    sequence deterministically; the seed is recorded in each result's `seed`
    field and execution order equals JSONL line order, so runs stay
    reproducible without extra machinery.
    """
    sequence = [
        (task_ref, run_index)
        for task_ref in task_refs
        for run_index in range(1, runs + 1)
    ]
    if shuffle_order is not None:
        random.Random(shuffle_order).shuffle(sequence)
    return sequence


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
    shuffle_order: int | None = None,
) -> list[dict]:
    outcomes = []
    for task_ref, run_index in _execution_order(task_refs, runs, shuffle_order):
        task_dir = task_ref if task_ref.is_dir() else task_ref.parent
        task = loader_mod.load_task(task_dir, repo_root)
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
    parser.add_argument(
        "--order",
        type=int,
        default=None,
        help="deterministically shuffle the task×run execution order with this "
        "seed (default: task-major order). Recorded via per-run seeds; JSONL "
        "line order equals execution order.",
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        default=None,
        help="Parent dir for per-run workspaces. Defaults to a fast local path "
        "outside the repo: each run clones a full repo and churns thousands of "
        "files, and doing that inside the OneDrive-synced tree (or under "
        "AppData\\Local) is throttled to a few files/s on Windows. Override "
        "with TRACE_WORK_ROOT when a specific volume is required.",
    )
    parser.add_argument("--exp-id", type=str, default=None)
    parser.add_argument("--runs-file", type=Path, default=None)
    parser.add_argument(
        "--mock-behavior",
        type=str,
        default=None,
        choices=["pass", "fail", "error", "slow", "memory"],
    )
    return parser


def _resolve_work_root(work_root: Path | None, repo_root: Path) -> Path:
    """Resolve the parent dir for per-run workspaces.

    Each run clones a full repo and churns thousands of files, so the *location*
    is a performance-critical choice on Windows: deletion under
    ``%LOCALAPPDATA%`` and inside the OneDrive-synced tree is throttled to a few
    files/s, which turns a ~12 s checkout into a multi-minute one. The default
    is therefore a fast local path outside the repo, overridable with
    ``TRACE_WORK_ROOT`` or ``--work-root``.
    """
    if work_root is not None:
        return work_root if work_root.is_absolute() else repo_root / work_root
    env = os.environ.get("TRACE_WORK_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("TEMP") or os.environ.get("TMP")
        if base:
            return (Path(base) / "trace_runs").resolve()
    return (repo_root / "runs" / "workspaces").resolve()


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
    work_root = _resolve_work_root(args.work_root, repo_root)
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
        args.order,
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
