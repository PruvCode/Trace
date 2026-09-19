"""End-to-end chain test: manifest -> adapter -> task.yaml -> TRACE loader.

This is the integration the corpus brief demands be *verified*, not assumed:
a manifest entry must materialize into a task directory that the existing TRACE
``loader`` accepts, and whose ``fixture`` resolves to a real repository the
runner can clone from.

The tests here use a generated manifest and a tiny generated fixture repo, so
they are fast and hermetic. The real SWE-bench fixture (the materialized Django
cache) is exercised separately by ``test_swebench_fixture.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmark import loader as loader_mod  # noqa: E402
from corpus import adapter  # noqa: E402

SEED_ENV = {
    "GIT_AUTHOR_NAME": "TRACE Test",
    "GIT_AUTHOR_EMAIL": "test@localhost",
    "GIT_COMMITTER_NAME": "TRACE Test",
    "GIT_COMMITTER_EMAIL": "test@localhost",
    "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00",
}

CANDIDATE_JSON = REPO_ROOT / "benchmark" / "validation" / "candidates"


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(SEED_ENV)
    env["GIT_CONFIG_PARAMETERS"] = "'core.autocrlf=false'"
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


@pytest.fixture()
def sweep_fixture(tmp_path: Path) -> tuple[Path, str]:
    """A 'swebench_repo' fixture repo inside a fake repo_root; returns (root, sha)."""
    root = tmp_path / "repo_root"
    fixture = root / "fixtures" / "swebench_repo"
    fixture.mkdir(parents=True)
    _git(fixture, "init", "-b", "main")
    _git(fixture, "config", "core.autocrlf", "false")
    (fixture / "django").mkdir()
    (fixture / "django" / "__init__.py").write_text(
        "__version__ = '4.1'\n", encoding="utf-8", newline=""
    )
    (fixture / "tests").mkdir()
    (fixture / "tests" / "runtests.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8", newline=""
    )
    _git(fixture, "add", "-A")
    _git(fixture, "commit", "-m", "base")
    return root, _git(fixture, "rev-parse", "HEAD")


def _manifest_task(base_commit: str, **over) -> dict:
    task = {
        "task_id": "django__django-99999",
        "source": "swe-bench",
        "source_task_id": "django__django-99999",
        "repo": "django/django",
        "base_commit": base_commit,
        "category": "B_structural",
        "problem_statement": "Fix the union-of-subquery ordering bug.",
        "evaluator": {
            "kind": "swebench_test_spec",
            "FAIL_TO_PASS": [
                "test_union_in_subquery (queries.test_qs_combinators.QuerySetSetOperationTests)"
            ],
            "PASS_TO_PASS": [],
            "runner": "tests/runtests.py",
        },
        "validation_status": {"valid": True},
        "selection_reason": "classified B_structural by patch shape",
        "memory_setup": None,
        "staleness_setup": None,
        "dataset": {"id": "princeton-nlp/SWE-bench", "revision": "r", "split": "test"},
        "version": "4.1",
    }
    task.update(over)
    return task


def test_adapter_fixture_points_at_a_resolvable_repo(sweep_fixture, tmp_path: Path):
    """The emitted fixture path must resolve to a real repo under repo_root.

    The loader resolves ``repo_root / task.fixture`` and requires it to be a
    directory; the runner additionally clones from it. A fixture value that does
    not exist is the exact gap that would keep corpus tasks out of the runner.
    """
    root, sha = sweep_fixture
    manifest = {"tasks": [_manifest_task(sha)]}
    created = adapter.materialize_manifest(manifest, root / "tasks")
    assert len(created) == 1

    meta = yaml.safe_load((created[0] / "task.yaml").read_text(encoding="utf-8"))
    fixture_dir = root / meta["fixture"]
    assert fixture_dir.is_dir(), f"fixture does not resolve: {fixture_dir}"
    # And it is a usable git clone containing the declared commit. `cat-file -e`
    # prints nothing, so success is asserted via the exit status, not stdout.
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=str(fixture_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def test_manifest_to_loader_end_to_end(sweep_fixture):
    """The real loader must accept each generated task.yaml unchanged."""
    root, sha = sweep_fixture
    manifest = {"tasks": [_manifest_task(sha)]}
    created = adapter.materialize_manifest(manifest, root / "tasks")

    cfg = loader_mod.load_task(created[0], root)
    assert cfg.task_id == "django__django-99999"
    assert cfg.category == "B_structural"
    assert cfg.base_commit == sha
    assert cfg.fixture == "fixtures/swebench_repo"
    # The eval command is the repo's own runner over normalized F2P labels.
    assert cfg.eval_command[0] == "python"
    assert cfg.eval_command[1] == "tests/runtests.py"
    assert (
        "queries.test_qs_combinators.QuerySetSetOperationTests.test_union_in_subquery"
        in cfg.eval_command
    )
    assert cfg.timeout_seconds > 0
    assert "swe-bench" in cfg.tags


def test_generated_tasks_are_discoverable_by_category(sweep_fixture):
    """Generated dirs must sit under their category so discovery finds them."""
    root, sha = sweep_fixture
    manifest = {"tasks": [_manifest_task(sha)]}
    created = adapter.materialize_manifest(manifest, root / "tasks")
    rel = created[0].relative_to(root / "tasks")
    assert rel.parts[0] == "B_structural"
    # A hand-rolled directory scan (as a discovery step would do) finds it.
    found = list((root / "tasks").glob("*/*/task.yaml"))
    assert len(found) == 1


def test_workspace_layer_accepts_the_generated_fixture(sweep_fixture):
    """workspace.ensure_fixture must resolve the adapter's fixture name.

    Regression guard for the integration gap: the runner calls
    ``ensure_fixture(repo_root / task.fixture)`` and raises
    'no seeder registered for fixture' for any name missing from _SEEDERS.
    """
    from benchmark import workspace

    root, sha = sweep_fixture
    assert "swebench_repo" in workspace._SEEDERS
    head = workspace.ensure_fixture(root / "fixtures" / "swebench_repo")
    assert head == sha


def test_workspace_creates_a_checkout_at_base_commit(sweep_fixture, tmp_path: Path):
    """The runner's clone-and-checkout must succeed for a generated task."""
    from benchmark import workspace

    root, sha = sweep_fixture
    dest = tmp_path / "workspace"
    ws = workspace.create_workspace(root / "fixtures" / "swebench_repo", sha, dest)
    assert ws.is_dir()
    assert workspace.workspace_head(ws) == sha


def test_real_validation_candidates_materialize_and_load():
    """Any *validated* candidate on disk must round-trip through the adapter.

    Skips when the probe has not produced a valid candidate yet, so the suite
    stays meaningful both before and after a probe run.
    """
    if not CANDIDATE_JSON.is_dir():
        pytest.skip("no validation results yet")
    valid = []
    for path in sorted(CANDIDATE_JSON.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("valid"):
            valid.append(data)
    if not valid:
        pytest.skip("no valid candidates on disk yet")

    # Build a manifest-shaped task from the first real validated candidate and
    # confirm the adapter accepts it (deterministic, no runner execution).
    import tempfile

    result = valid[0]
    task = _manifest_task(
        result["base_commit"],
        task_id=result["task_id"],
        source_task_id=result["source_task_id"],
        evaluator={
            "kind": "swebench_test_spec",
            "FAIL_TO_PASS": result.get("f2p_labels") or [],
            "PASS_TO_PASS": result.get("p2p_labels") or [],
            "runner": "tests/runtests.py",
        },
        version=result.get("version"),
    )
    if not task["evaluator"]["FAIL_TO_PASS"]:
        pytest.skip("candidate has no recorded F2P labels")

    with tempfile.TemporaryDirectory() as tmp:
        created = adapter.materialize_manifest({"tasks": [task]}, Path(tmp))
        text = (created[0] / "task.yaml").read_text(encoding="utf-8")
        assert 'task_id: ' + result["task_id"] in text
        assert "tests/runtests.py" in text


def test_workspace_timeouts_cover_a_full_swebench_clone():
    """Clone/checkout budgets must exceed the measured cost of this corpus.

    A materialized Django clone is ~350 MB and its checkout writes ~6,600
    files; with on-access antivirus scanning that costs minutes. The runner's
    120 s metadata budget would kill a healthy clone, so the tree-materializing
    operations must use their own, larger budget. This test pins that
    separation so it cannot silently regress.
    """
    from benchmark import workspace

    assert workspace.CLONE_TIMEOUT >= 1800
    assert workspace.CHECKOUT_TIMEOUT >= 1800
    # Metadata calls stay tight: a plain `git rev-parse` must not wait 30 min.
    assert workspace.GIT_TIMEOUT <= 300
    assert workspace.CLONE_TIMEOUT > workspace.GIT_TIMEOUT


def test_runner_passes_base_commit_to_the_fixture_seeder():
    """The runner must tell the shared fixture which commit the task needs.

    Generated fixtures return their only commit, but the SWE-bench clone is a
    multi-commit historical clone whose HEAD is not the task base_commit. If the
    runner did not pass base_commit, the seeder could not verify the historical
    commit exists locally and the chain would fail on a HEAD mismatch.
    """
    import inspect

    from benchmark import runner

    src = inspect.getsource(runner)
    assert "base_commit=task.base_commit" in src, (
        "runner no longer tells ensure_fixture which commit to verify"
    )


def test_workspace_timeouts_cover_a_full_swebench_clone():
    """Clone/checkout budgets must exceed the measured cost of this corpus.

    A materialized Django clone is ~350 MB and its checkout writes ~6,600
    files; with on-access antivirus scanning that costs minutes. The runner's
    120 s metadata budget would kill a healthy clone, so the tree-materializing
    operations must use their own, larger budget. This test pins that
    separation so it cannot silently regress.
    """
    from benchmark import workspace

    assert workspace.CLONE_TIMEOUT >= 1800
    assert workspace.CHECKOUT_TIMEOUT >= 1800
    # Metadata calls stay tight: a plain `git rev-parse` must not wait 30 min.
    assert workspace.GIT_TIMEOUT <= 300
    assert workspace.CLONE_TIMEOUT > workspace.GIT_TIMEOUT


def test_runner_passes_base_commit_to_the_fixture_seeder():
    """The runner must tell the shared fixture which commit the task needs.

    Generated fixtures return their only commit, but the SWE-bench clone is a
    multi-commit historical clone whose HEAD is not the task base_commit. If the
    runner did not pass base_commit, the seeder could not verify the historical
    commit exists locally and the chain would fail on a HEAD mismatch.
    """
    import inspect

    from benchmark import runner

    src = inspect.getsource(runner)
    assert "base_commit=task.base_commit" in src, (
        "runner no longer tells ensure_fixture which commit to verify"
    )


def test_runner_default_work_root_avoids_slow_paths(monkeypatch, tmp_path):
    """Per-run workspaces must default to a fast local path.

    Each run clones a full repo and churns thousands of files. On Windows both
    the OneDrive-synced repo tree and %LOCALAPPDATA% throttle deletion to a few
    files/s, so defaulting there would turn a ~12 s checkout into minutes. The
    default must therefore resolve outside the repo, and an explicit override
    must still win.
    """
    from benchmark import runner

    fake_temp = tmp_path / "fasttemp"
    fake_temp.mkdir()
    monkeypatch.setenv("TEMP", str(fake_temp))
    monkeypatch.setenv("TMP", str(fake_temp))
    monkeypatch.delenv("TRACE_WORK_ROOT", raising=False)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    default = runner._resolve_work_root(None, repo_root)
    assert repo_root not in default.parents, default
    assert str(default).startswith(str(fake_temp.resolve()))

    # Explicit --work-root wins.
    explicit = runner._resolve_work_root(tmp_path / "explicit", repo_root)
    assert explicit == tmp_path / "explicit"

    # Explicit relative --work-root stays inside the repo (reproducibility).
    rel = runner._resolve_work_root(Path("runs/workspaces"), repo_root)
    assert rel == repo_root / "runs" / "workspaces"

    # TRACE_WORK_ROOT is honoured when no flag is given.
    monkeypatch.setenv("TRACE_WORK_ROOT", str(tmp_path / "envroot"))
    assert runner._resolve_work_root(None, repo_root) == (tmp_path / "envroot")


def test_runner_default_work_root_avoids_slow_paths(monkeypatch, tmp_path):
    """Per-run workspaces must default to a fast local path.

    Each run clones a full repo and churns thousands of files. On Windows both
    the OneDrive-synced repo tree and %LOCALAPPDATA% throttle deletion to a few
    files/s, so defaulting there would turn a ~12 s checkout into minutes. The
    default must therefore resolve outside the repo, and an explicit override
    must still win.
    """
    from benchmark import runner

    fake_temp = tmp_path / "fasttemp"
    fake_temp.mkdir()
    monkeypatch.setenv("TEMP", str(fake_temp))
    monkeypatch.setenv("TMP", str(fake_temp))
    monkeypatch.delenv("TRACE_WORK_ROOT", raising=False)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    default = runner._resolve_work_root(None, repo_root)
    assert repo_root not in default.parents, default
    assert str(default).startswith(str(fake_temp.resolve()))

    # Explicit --work-root wins.
    explicit = runner._resolve_work_root(tmp_path / "explicit", repo_root)
    assert explicit == tmp_path / "explicit"

    # Explicit relative --work-root stays inside the repo (reproducibility).
    rel = runner._resolve_work_root(Path("runs/workspaces"), repo_root)
    assert rel == repo_root / "runs" / "workspaces"

    # TRACE_WORK_ROOT is honoured when no flag is given.
    monkeypatch.setenv("TRACE_WORK_ROOT", str(tmp_path / "envroot"))
    assert runner._resolve_work_root(None, repo_root) == (tmp_path / "envroot")
