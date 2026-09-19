"""Reproducible descriptive analysis of benchmark JSONL (Phase 6).

Reads raw result rows, groups by (task_id, configuration), and reports
descriptive summaries plus paired baseline-vs-reference deltas. Raw rows
stay the source of truth: every figure cites its n, and infrastructure
failures are classified and listed, never silently dropped.

Deliberately no inferential statistics: with pilot-scale samples that
machinery would manufacture significance. Success rates are computed over
decided runs only (infra failures excluded from the denominator AND shown
alongside, so nothing is hidden).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from benchmark import loader as loader_mod
from benchmark import results as results_mod

INFRA_ERROR_PREFIXES = (
    "workspace_setup_failed",
    "memory_setup_failed",
    "evaluator_failed",
    "llm_auth_missing",
    "llm_provider_error",
)

TIMEOUT_MARKERS = ("agent_timeout:", "agent exceeded budget.timeout_seconds=")


def classify_outcome(row: dict) -> str:
    """One of: success | task_fail | agent_error | timeout | provider_error | infra_error.

    Deterministic mapping from recorded fields. Infrastructure failures come
    first: a run whose memory backend never started is not evidence about
    the task, even if the evaluator happened to pass the clean tree.
    """
    error = row.get("error") or ""
    termination = row.get("termination_reason") or ""
    if error.startswith(INFRA_ERROR_PREFIXES):
        if error.startswith(("llm_auth_missing:", "llm_provider_error:")):
            return "provider_error"
        return "infra_error"
    if error.startswith("agent_timeout:") or termination.startswith(
        "agent exceeded budget.timeout_seconds="
    ):
        return "timeout"
    if error.startswith("agent_failed:"):
        return "agent_error"
    if row.get("success") is True:
        return "success"
    return "task_fail"


def check_row_integrity(row: dict) -> list[str]:
    """Per-row violations: schema, token invariant. Empty means clean."""
    problems = []
    try:
        results_mod.validate_result(row)
    except ValueError as exc:
        problems.append(str(exc))
        return problems
    in_tok, out_tok, total = (
        row.get("input_tokens"),
        row.get("output_tokens"),
        row.get("total_tokens"),
    )
    if in_tok is not None and out_tok is not None:
        if total != in_tok + out_tok:
            problems.append(
                f"token invariant broken: {in_tok} + {out_tok} != {total}"
            )
        if row.get("token_source") != "provider":
            problems.append("exact usage recorded but token_source != provider")
    else:
        if total is not None:
            problems.append("partial usage must leave total_tokens None")
        if row.get("token_source") != "unknown":
            problems.append("unknown usage must report token_source unknown")
    return problems


def _stats(values: list[float | int]) -> dict | None:
    if not values:
        return None
    total = sum(values)
    return {
        "n": len(values),
        "sum": total,
        "mean": total / len(values),
        "min": min(values),
        "max": max(values),
    }


def summarize_group(task_id: str, configuration: str, rows: list[dict]) -> dict:
    """Descriptive summary. Token figures cover provider-measured rows only."""
    classes = [classify_outcome(r) for r in rows]
    decided = [r for r, c in zip(rows, classes) if c not in ("infra_error", "provider_error")]
    successes = sum(1 for r in decided if r.get("success") is True)
    measured = [r for r in decided if r.get("token_source") == "provider"]
    sources = sorted({r.get("token_source") for r in decided})
    return {
        "task_id": task_id,
        "configuration": configuration,
        "n": len(rows),
        "decided_n": len(decided),
        "success": successes,
        "success_rate": (successes / len(decided)) if decided else None,
        "classes": {c: classes.count(c) for c in sorted(set(classes))},
        "infra_rows": [
            {"run": r.get("run"), "class": c, "error": (r.get("error") or "")[:160]}
            for r, c in zip(rows, classes)
            if c in ("infra_error", "provider_error")
        ],
        "token_sources": sources,
        "total_tokens": _stats([r["total_tokens"] for r in measured]),
        "latency_seconds": _stats([r["latency_seconds"] for r in decided]),
        "turns": _stats([r["turns"] for r in decided]),
        "tool_calls": _stats([r["tool_calls"] for r in decided]),
        "memory_tool_calls": _stats([r.get("memory_tool_calls", 0) for r in decided]),
    }


def group_rows(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row["task_id"], row["configuration"]), []).append(row)
    return groups


def paired_delta(task_id: str, base: dict, other: dict) -> dict:
    """Descriptive baseline-vs-other delta for one task. No significance."""

    def _metric_diff(a, b):
        out = {}
        for key in ("sum", "mean"):
            x = (a or {}).get(key)
            y = (b or {}).get(key)
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                out[key] = y - x
            else:
                out[key] = None
        return out

    def _num_diff(x, y):
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            return y - x
        return None

    return {
        "task_id": task_id,
        "baseline": base["configuration"],
        "other": other["configuration"],
        "success_diff": (other["success"] or 0) - (base["success"] or 0),
        "success_rate_diff": _num_diff(base.get("success_rate"), other.get("success_rate")),
        "total_tokens": _metric_diff(base.get("total_tokens"), other.get("total_tokens")),
        "latency_seconds": _metric_diff(base.get("latency_seconds"), other.get("latency_seconds")),
        "turns": _metric_diff(base.get("turns"), other.get("turns")),
        "tool_calls": _metric_diff(base.get("tool_calls"), other.get("tool_calls")),
    }


def task_categories(tasks_root: Path | None, repo_root: Path) -> dict[str, str]:
    """Map task_id -> category by loading the corpus. Unknown ids stay unknown."""
    mapping: dict[str, str] = {}
    if tasks_root is None:
        return mapping
    root = tasks_root if tasks_root.is_absolute() else repo_root / tasks_root
    for task_yaml in sorted(root.rglob("task.yaml")):
        if any(part.startswith("_") for part in task_yaml.parent.relative_to(root).parts):
            continue
        try:
            task = loader_mod.load_task(task_yaml.parent, repo_root)
        except Exception:  # noqa: BLE001 - unloadable tasks are simply unmapped
            continue
        mapping[task.task_id] = task.category
    return mapping


def check_fairness(rows: list[dict]) -> list[str]:
    """Spot-check: one base commit, prompt, and evaluator per task."""
    problems = []
    by_task: dict[str, list[dict]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], []).append(row)
    for task_id in sorted(by_task):
        group = by_task[task_id]
        for field in ("base_commit", "prompt_sha256"):
            distinct = {json.dumps(r.get(field), sort_keys=True) for r in group}
            if len(distinct) > 1:
                problems.append(f"{task_id}: {field} differs across configurations")
        commands = {json.dumps(r.get("eval_command")) for r in group}
        if len(commands) > 1:
            problems.append(f"{task_id}: eval_command differs across configurations")
    return problems


def analyze(
    rows: list[dict],
    categories: dict[str, str] | None = None,
) -> dict:
    """Full reproducible analysis artifact. Pure function of raw rows."""
    categories = categories or {}
    integrity = []
    for index, row in enumerate(rows):
        for problem in check_row_integrity(row):
            integrity.append(f"row {index} ({row.get('task_id')}): {problem}")
    groups = [
        summarize_group(task_id, configuration, items)
        for (task_id, configuration), items in sorted(group_rows(rows).items())
    ]
    pairs = []
    by_task: dict[str, dict[str, dict]] = {}
    for summary in groups:
        by_task.setdefault(summary["task_id"], {})[summary["configuration"]] = summary
    for task_id in sorted(by_task):
        if "baseline" in by_task[task_id]:
            for configuration in sorted(by_task[task_id]):
                if configuration != "baseline":
                    pairs.append(
                        paired_delta(
                            task_id, by_task[task_id]["baseline"], by_task[task_id][configuration]
                        )
                    )
    by_category: dict[str, list[dict]] = {}
    for summary in groups:
        by_category.setdefault(
            categories.get(summary["task_id"], "unknown"), []
        ).append(summary)
    category_summaries = {}
    for category in sorted(by_category):
        decided = sum(s["decided_n"] for s in by_category[category])
        successes = sum(s["success"] for s in by_category[category])
        category_summaries[category] = {
            "groups": len(by_category[category]),
            "decided_n": decided,
            "success": successes,
            "success_rate": (successes / decided) if decided else None,
        }
    total_decided = sum(s["decided_n"] for s in groups)
    total_success = sum(s["success"] for s in groups)
    return {
        "n_rows": len(rows),
        "n_groups": len(groups),
        "integrity_violations": integrity,
        "fairness_violations": check_fairness(rows),
        "groups": groups,
        "pairs": pairs,
        "categories": category_summaries,
        "overall": {
            "decided_n": total_decided,
            "success": total_success,
            "success_rate": (total_success / total_decided) if total_decided else None,
        },
    }


def render_text(report: dict) -> str:
    """Human-readable rendering. Descriptive figures only, every n cited."""
    lines = [
        "TRACE analysis (descriptive only: counts and rates with n cited, no inferential statistics)",
        f"rows={report['n_rows']} groups={report['n_groups']}",
        "",
    ]
    if report["integrity_violations"]:
        lines.append("INTEGRITY VIOLATIONS:")
        lines.extend(f"  ! {v}" for v in report["integrity_violations"])
        lines.append("")
    if report["fairness_violations"]:
        lines.append("FAIRNESS VIOLATIONS:")
        lines.extend(f"  ! {v}" for v in report["fairness_violations"])
        lines.append("")
    lines.append("PER-TASK × CONFIG:")
    for summary in sorted(
        report["groups"], key=lambda s: (s["task_id"], s["configuration"])
    ):
        rate = summary["success_rate"]
        rate_text = f"{rate:.2f}" if rate is not None else "n/a"
        tokens = summary["total_tokens"]
        token_text = (
            f"tokens(sum={tokens['sum']},mean={tokens['mean']:.0f},n={tokens['n']})"
            if tokens
            else "tokens(unmeasured)"
        )
        lines.append(
            f"  {summary['task_id']} {summary['configuration']}: "
            f"success={summary['success']}/{summary['decided_n']} "
            f"rate={rate_text} {token_text} classes={summary['classes']}"
        )
        for infra in summary["infra_rows"]:
            lines.append(f"    infra run={infra['run']} {infra['class']}: {infra['error']}")
    lines.append("")
    lines.append("PAIRED DELTAS (other minus baseline, descriptive):")
    if not report["pairs"]:
        lines.append("  (no baseline-paired groups)")
    for pair in report["pairs"]:
        lines.append(
            f"  {pair['task_id']} {pair['other']}: "
            f"success_diff={pair['success_diff']} "
            f"rate_diff={pair['success_rate_diff']} "
            f"tokens={pair['total_tokens']} latency={pair['latency_seconds']}"
        )
    lines.append("")
    lines.append("CATEGORIES:")
    for category in sorted(report["categories"]):
        summary = report["categories"][category]
        rate = summary["success_rate"]
        lines.append(
            f"  {category}: groups={summary['groups']} "
            f"success={summary['success']}/{summary['decided_n']} "
            f"rate={'%.2f' % rate if rate is not None else 'n/a'}"
        )
    overall = report["overall"]
    rate = overall["success_rate"]
    lines.append(
        f"OVERALL: success={overall['success']}/{overall['decided_n']} "
        f"rate={'%.2f' % rate if rate is not None else 'n/a'}"
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TRACE results analysis (descriptive only)")
    parser.add_argument("--results", type=Path, required=True, help="results JSONL path")
    parser.add_argument(
        "--tasks-root",
        type=Path,
        default=None,
        help="corpus root for task_id -> category mapping (optional)",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    rows = results_mod.read_results(args.results)
    categories = task_categories(args.tasks_root, repo_root)
    report = analyze(rows, categories)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(render_text(report), end="")
    if report["integrity_violations"] or report["fairness_violations"]:
        print("analysis found violations (see above)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
