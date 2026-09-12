"""Baseline vs reference-memory fairness: equal controls, explicit intervention.

These tests fail loudly if any control dimension drifts between the two
Phase 4 configurations. Memory availability (+ the fixed tool-availability
addendum) is the only permitted difference.
"""

import copy

import pytest

from agent.tools import build_core_tools
from benchmark import fairness as fairness_mod
from benchmark import loader as loader_mod


def _load(repo_root):
    task = loader_mod.load_task(
        repo_root / "tasks" / "A_control" / "task_01_timeout_fix", repo_root
    )
    baseline = loader_mod.load_config(repo_root / "configs" / "baseline.yaml")
    reference = loader_mod.load_config(repo_root / "configs" / "reference_memory.yaml")
    return task, baseline, reference


def test_controls_equal_intervention_explicit(repo_root):
    task, baseline, reference = _load(repo_root)
    base_spec = fairness_mod.resolve_run_spec(task, baseline, str(repo_root))
    ref_spec = fairness_mod.resolve_run_spec(task, reference, str(repo_root))
    message = fairness_mod.assert_fair(base_spec, ref_spec)
    assert "fair" in message
    # The intervention, and nothing else.
    assert baseline["backend"] is None
    assert reference["backend"] == "reference"
    assert baseline.get("prompt_addendum", "") == ""
    assert "find_definition" in reference["prompt_addendum"]
    assert "verify stale" not in reference["prompt_addendum"].lower()


def test_control_dimensions_identical(repo_root):
    _task, baseline, reference = _load(repo_root)
    for field in (
        "agent",
        "model",
        "model_parameters",
        "budget",
    ):
        assert baseline[field] == reference[field], field


def test_control_mutation_trips(repo_root):
    task, baseline, reference = _load(repo_root)
    base_spec = fairness_mod.resolve_run_spec(task, baseline, str(repo_root))
    for mutate in (
        lambda c: c.update(model="other"),
        lambda c: c.update(budget={**c["budget"], "max_turns": 999}),
    ):
        altered = copy.deepcopy(reference)
        mutate(altered)
        altered_spec = fairness_mod.resolve_run_spec(task, altered, str(repo_root))
        with pytest.raises(fairness_mod.FairnessError):
            fairness_mod.assert_fair(base_spec, altered_spec)


def test_prompt_base_shared(repo_root, tmp_path):
    task, _baseline, _reference = _load(repo_root)
    assert task.prompt_sha256
    # Runner builds prompt as base + addendum; base bytes are one object.
    _ = tmp_path
    assert task.prompt == (
        (repo_root / task.task_dir / task.prompt_file).read_text(encoding="utf-8")
    )


def test_core_tools_config_independent(tmp_path):
    first = {t.name: (t.description, t.json_schema) for t in build_core_tools(tmp_path)}
    second = {t.name: (t.description, t.json_schema) for t in build_core_tools(tmp_path)}
    assert first == second
    assert set(first) == {"read_file", "write_file", "done"}


def test_memory_key_validated(repo_root, tmp_path):
    cfg = loader_mod.load_config(repo_root / "configs" / "reference_memory.yaml")
    assert cfg["memory"] == {"preseed": []}
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: x\nbackend: reference\nagent: mock\nmodel: mock\n"
        "budget: {max_turns: 1, max_tool_calls: 1, timeout_seconds: 1}\n"
        "memory: [not, a, dict]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="memory must be a mapping"):
        loader_mod.load_config(bad)
