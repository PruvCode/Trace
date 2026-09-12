"""Analysis unit tests: synthetic rows, no models, no network."""

import json
from pathlib import Path

import pytest

from benchmark import analyze as analyze_mod


def _row(**over):
    row = {
        "task_id": "task_01_timeout_fix",
        "configuration": "baseline",
        "run": 1,
        "base_commit": "abc123",
        "success": True,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "latency_seconds": 1.5,
        "turns": 3,
        "tool_calls": 3,
        "memory_tool_calls": 0,
        "token_source": "unknown",
        "seed": "0-1",
        "model": "mock",
        "agent": "mock",
        "task_version": "v1",
        "config_hash": "c1",
        "eval_command": ["python", "-m", "pytest"],
        "exit_code": 0,
        "workspace": "ws",
        "prompt_sha256": "p1",
        "fixture": "fixtures/toy_repo",
        "error": None,
        "termination_reason": "done_tool_called",
        "timed_out": False,
        "tool_log": [],
        "git_status": [],
        "timestamp": "2026-01-01T00:00:00+00:00",
        "benchmark_version": "0.1.0",
    }
    row.update(over)
    return row


def test_classify_outcome_taxonomy():
    c = analyze_mod.classify_outcome
    assert c(_row()) == "success"
    assert c(_row(success=False)) == "task_fail"
    assert c(_row(success=False, error="agent_failed: boom")) == "agent_error"
    assert c(_row(success=False, error="agent_timeout: x")) == "timeout"
    assert (
        c(_row(success=False, termination_reason="agent exceeded budget.timeout_seconds=1"))
        == "timeout"
    )
    assert c(_row(success=False, error="llm_auth_missing: k")) == "provider_error"
    assert c(_row(success=False, error="llm_provider_error: k")) == "provider_error"
    assert c(_row(success=False, error="workspace_setup_failed: k")) == "infra_error"
    assert c(_row(success=False, error="memory_setup_failed: k")) == "infra_error"
    assert c(_row(success=False, error="evaluator_failed: k")) == "infra_error"
    # Infrastructure failure is not task evidence, even if the evaluator passed.
    assert c(_row(success=True, error="memory_setup_failed: k")) == "infra_error"


def test_check_row_integrity():
    assert analyze_mod.check_row_integrity(_row()) == []
    ok = _row(input_tokens=10, output_tokens=5, total_tokens=15, token_source="provider")
    assert analyze_mod.check_row_integrity(ok) == []
    bad_total = _row(input_tokens=10, output_tokens=5, total_tokens=16, token_source="provider")
    assert any("invariant" in p for p in analyze_mod.check_row_integrity(bad_total))
    bad_source = _row(input_tokens=10, output_tokens=5, total_tokens=15, token_source="unknown")
    assert any("token_source" in p for p in analyze_mod.check_row_integrity(bad_source))
    partial = _row(input_tokens=10, total_tokens=10)
    assert any("total_tokens None" in p for p in analyze_mod.check_row_integrity(partial))
    missing = _row()
    del missing["success"]
    assert any("required" in p for p in analyze_mod.check_row_integrity(missing))


def test_summarize_group_rates_and_token_split():
    rows = [
        _row(run=1, success=True),
        _row(run=2, success=False),
        _row(run=3, success=False, error="memory_setup_failed: k"),
        _row(
            run=4, success=True, input_tokens=100, output_tokens=50,
            total_tokens=150, token_source="provider",
        ),
    ]
    summary = analyze_mod.summarize_group("t", "baseline", rows)
    assert summary["n"] == 4
    assert summary["decided_n"] == 3
    assert summary["success"] == 2
    assert summary["success_rate"] == pytest.approx(2 / 3)
    assert summary["classes"]["infra_error"] == 1
    assert len(summary["infra_rows"]) == 1
    # Token figures cover the single provider-measured row only.
    assert summary["token_sources"] == ["provider", "unknown"]
    assert summary["total_tokens"]["n"] == 1
    assert summary["total_tokens"]["sum"] == 150


def test_summarize_group_empty_decided():
    summary = analyze_mod.summarize_group(
        "t", "baseline", [_row(run=1, success=False, error="memory_setup_failed: k")]
    )
    assert summary["decided_n"] == 0
    assert summary["success_rate"] is None
    assert summary["total_tokens"] is None


def test_paired_delta():
    base = analyze_mod.summarize_group("t", "baseline", [_row(run=1, success=True)])
    other = analyze_mod.summarize_group(
        "t", "reference_memory",
        [_row(run=1, success=False),
         _row(run=1, configuration="reference_memory", success=True,
               input_tokens=10, output_tokens=5, total_tokens=15, token_source="provider")],
    )
    delta = analyze_mod.paired_delta("t", base, other)
    assert delta["success_diff"] == 0  # 1 - 1
    assert delta["total_tokens"] == {"sum": None, "mean": None}  # baseline unmeasured
    assert delta["latency_seconds"]["mean"] is not None


def test_analyze_end_to_end_and_violations():
    rows = [
        _row(task_id="a", configuration="baseline", run=1, success=True),
        _row(task_id="a", configuration="baseline", run=2, success=False),
        _row(task_id="a", configuration="reference_memory", run=1, success=True),
        _row(task_id="b", configuration="baseline", run=1, success=False,
             prompt_sha256="DIFFERENT"),
        _row(task_id="b", configuration="reference_memory", run=1, success=False),
    ]
    report = analyze_mod.analyze(rows, {"a": "A_control", "b": "B_structural"})
    assert report["n_rows"] == 5
    assert report["n_groups"] == 4
    assert len(report["pairs"]) == 2  # tasks a and b both have both configs
    assert set(report["categories"]) == {"A_control", "B_structural"}
    assert report["overall"]["decided_n"] == 5
    assert any("prompt_sha256" in v for v in report["fairness_violations"])
    text = analyze_mod.render_text(report)
    assert "FAIRNESS VIOLATIONS" in text
    assert "PAIRED DELTAS" in text


def test_unknown_category():
    report = analyze_mod.analyze([_row(task_id="zzz")], {})
    assert "unknown" in report["categories"]


def test_cli_exit_codes(tmp_path):
    clean = tmp_path / "clean.jsonl"
    clean.write_text(json.dumps(_row()) + "\n", encoding="utf-8")
    assert analyze_mod.main(["--results", str(clean), "--format", "json"]) == 0
    assert analyze_mod.main(["--results", str(clean)]) == 0
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps(_row(task_id="a", configuration="baseline", prompt_sha256="p1")) + "\n"
        + json.dumps(_row(task_id="a", configuration="reference_memory", prompt_sha256="p2")) + "\n",
        encoding="utf-8",
    )
    assert analyze_mod.main(["--results", str(bad)]) == 2


def test_task_categories_mapping(repo_root):
    mapping = analyze_mod.task_categories(Path("tasks"), repo_root)
    assert mapping.get("task_01_timeout_fix") == "A_control"
    assert all(isinstance(v, str) and v for v in mapping.values())
