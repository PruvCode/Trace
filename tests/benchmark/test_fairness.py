"""Fairness tests: controls gate comparisons; only memory may differ."""

import copy

import pytest

from benchmark import fairness as fairness_mod
from benchmark import loader as loader_mod


def _specs(repo_root):
    task = loader_mod.load_task(
        repo_root / "tasks" / "A_control" / "task_01_timeout_fix", repo_root
    )
    config = loader_mod.load_config(repo_root / "configs" / "baseline.yaml")
    spec = fairness_mod.resolve_run_spec(task, config, str(repo_root))
    return task, config, spec


def test_self_fair(repo_root):
    _, _, spec = _specs(repo_root)
    assert "fair" in fairness_mod.assert_fair(spec, dict(spec))


def test_model_change_trips(repo_root):
    _, _, spec = _specs(repo_root)
    other = dict(spec, model="different-model")
    with pytest.raises(fairness_mod.FairnessError):
        fairness_mod.assert_fair(spec, other)


def test_budget_change_trips(repo_root):
    _, _, spec = _specs(repo_root)
    other = dict(spec)
    other["budget"] = dict(spec["budget"], max_turns=999)
    with pytest.raises(fairness_mod.FairnessError):
        fairness_mod.assert_fair(spec, other)


def test_prompt_change_trips(repo_root):
    _, _, spec = _specs(repo_root)
    other = dict(spec, prompt_sha256="0" * 64)
    with pytest.raises(fairness_mod.FairnessError):
        fairness_mod.assert_fair(spec, other)


def test_eval_change_trips(repo_root):
    _, _, spec = _specs(repo_root)
    other = dict(spec, eval_command=["python", "-m", "pytest", "-q"])
    with pytest.raises(fairness_mod.FairnessError):
        fairness_mod.assert_fair(spec, other)


def test_backend_difference_allowed(repo_root):
    """Proves the intervention seam: backend/addendum may differ, nothing else."""
    task, config, spec = _specs(repo_root)
    other_config = copy.deepcopy(config)
    other_config["backend"] = "reference"
    other_config["prompt_addendum"] = "Memory tools available."
    other = fairness_mod.resolve_run_spec(task, other_config, str(repo_root))
    assert "fair" in fairness_mod.assert_fair(spec, other)
