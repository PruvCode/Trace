"""Manifest -> TRACE task adapter.

The corpus pipeline produces a machine-readable manifest
(``benchmark/tasks/manifest.json``). The TRACE engine, by contrast, consumes
runnable task *directories* (``tasks/<category>/<id>/task.yaml`` + prompt).
This module is the single glue layer between the two: it materializes each
manifest entry into a loader-valid task directory so the existing
``benchmark.runner`` / ``benchmark.loader`` / ``benchmark.evaluator``
abstractions run it unchanged.

Design constraints (from the task brief):
- no second benchmark framework: we reuse ``loader.load_task`` for validation
  and the existing ``task.yaml`` schema verbatim;
- no overwriting the previous session's untracked work: generation is
  idempotent and refuses to clobber a directory it does not own;
- deterministic: identical manifest -> byte-identical task files;
- the manifest stays the source of truth; generated task dirs are derived
  artifacts and carry a marker file so they are identifiable.

SWE-bench-specific note: a SWE-bench instance is evaluated by running the
repository's own test runner over the test spec (FAIL_TO_PASS / PASS_TO_PASS)
after applying the repo at ``base_commit``. The evaluator command emitted here
is therefore Django's ``tests/runtests.py`` invocation over FAIL_TO_PASS, and
``fixture``/``base_commit`` point at the *cached external clone*, not a local
fixture repo. That is a deliberate difference from the synthetic A/B/C/D
tasks and is recorded in the task's ``tags`` and ``source`` fields.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from corpus import validate

# Marker file: present only in directories this adapter generated, so we can
# safely refuse to overwrite anything else.
GENERATED_MARKER = ".trace_generated.json"

ADAPTER_VERSION = "2"

# The TRACE runner resolves a task's fixture as ``repo_root / task.fixture`` and
# requires it to be a directory it can clone from. SWE-bench's upstream repo is
# ``django/django``, but that string is NOT a valid repo-relative path inside
# the TRACE checkout (there is no ``django/django`` directory here). The corpus
# therefore exposes the materialized external clone at this repo-relative
# location -- see ``scripts/link_swebench_fixture.cmd``.
SWEBENCH_FIXTURE = "fixtures/swebench_repo"

# upstream repo -> repo-relative fixture directory
_FIXTURE_BY_REPO = {
    "django/django": SWEBENCH_FIXTURE,
}

# A deterministic, category-neutral slug for generated directories. Keeps the
# upstream instance id verbatim (it is already filesystem-safe) but namespaces
# it so it cannot collide with the synthetic corpus tasks.
_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slug(instance_id: str) -> str:
    return _NAME_RE.sub("_", instance_id).strip("._-") or "task"


def generated_task_dir(tasks_root: Path, task: dict) -> Path:
    """Where a manifest entry's runnable directory lives (category-scoped)."""
    return tasks_root / task["category"] / _slug(task["task_id"])


def fixture_for_repo(repo: str) -> str:
    """Repo-relative fixture directory for an upstream repo.

    The loader/runner resolve ``fixture`` against the TRACE repo root, so the
    upstream name (``django/django``) cannot be used verbatim -- no such
    directory exists here. The fixture must therefore be the repo-relative
    bridge path under ``fixtures/``. Unknown repos still get a deterministic
    ``fixtures/<name>`` path so the failure is a clear "missing fixture" rather
    than a silently wrong clone.
    """
    if repo in _FIXTURE_BY_REPO:
        return _FIXTURE_BY_REPO[repo]
    return f"fixtures/{_slug(repo)}"


def _prompt_text(task: dict) -> str:
    """Deterministic prompt = problem statement + mechanical contract.

    The prompt states the task and the evaluation contract but never names the
    fix, the failing tests' internals, or any memory tool — identical for
    baseline and reference runs (fairness).
    """
    statement = (task.get("problem_statement") or "").strip()
    return (
        f"# {task['task_id']}\n\n"
        f"{statement}\n\n"
        "You are working in the Django source tree at the pinned base commit. "
        "Fix the issue described above by changing the source code. You are "
        "done when the repository's own test suite passes for the affected "
        "tests. Do not just claim the fix works — the benchmark checks the "
        "behavior mechanically.\n"
    )


def _eval_command(task: dict) -> list[str]:
    """Django test-runner invocation over FAIL_TO_PASS labels.

    Labels from the SWE-bench spec are in unittest display form
    (``method (module.Class)``) and are converted to the importable
    ``module.Class.method`` form that ``runtests.py`` accepts. Non-test prose
    fragments that also occur in the field are dropped. An empty resulting list
    is a hard error: a task with no runnable objective evaluator must not be
    materialized.
    """
    spec = task.get("evaluator") or {}
    labels = validate.normalize_test_labels(spec.get("FAIL_TO_PASS") or [])
    if not labels:
        raise ValueError(
            f"{task['task_id']}: no runnable FAIL_TO_PASS labels; refusing to "
            "materialize a task without an objective evaluator"
        )
    return [
        "python",
        "tests/runtests.py",
        *labels,
        "-v",
        "0",
        "--parallel",
        "1",
    ]


def task_yaml_text(task: dict) -> str:
    """Render the existing TRACE ``task.yaml`` schema as deterministic YAML.

    Written by hand (not ``yaml.dump``) so key order and quoting are stable,
    which keeps the generated file byte-identical across runs.
    """
    eval_cmd = _eval_command(task)
    lines = [
        f"task_id: {task['task_id']}",
        f"category: {task['category']}",
        # fixture is a repo-relative path the loader resolves and the runner
        # clones from. For SWE-bench that is the bridge to the external cache,
        # never the upstream 'org/name' string.
        f"fixture: {fixture_for_repo(task['repo'])}",
        f'base_commit: "{task["base_commit"]}"',
        "prompt_file: prompt.md",
        "eval_command:",
    ]
    lines += [f"  - {json.dumps(part)}" for part in eval_cmd]
    lines += [
        "timeout_seconds: 1800",
        "tags:",
    ]
    tags = ["swe-bench", task["category"], task["repo"]]
    for tag in tags:
        lines.append(f"  - {json.dumps(tag)}")
    return "\n".join(lines) + "\n"


def is_generated(task_dir: Path) -> bool:
    return (task_dir / GENERATED_MARKER).is_file()


def materialize_task(
    task: dict,
    tasks_root: Path,
    *,
    force: bool = False,
) -> Path:
    """Write one manifest entry as a loader-valid task directory.

    Refuses to overwrite a directory that exists without our marker (i.e. a
    hand-written or previous-session task) unless ``force`` is set. Returns the
    created directory.
    """
    dest = generated_task_dir(tasks_root, task)
    if dest.exists() and not is_generated(dest) and not force:
        raise FileExistsError(
            f"refusing to overwrite non-generated task dir: {dest}"
        )
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "prompt.md").write_text(_prompt_text(task), encoding="utf-8", newline="\n")
    (dest / "task.yaml").write_text(
        task_yaml_text(task), encoding="utf-8", newline="\n"
    )
    (dest / GENERATED_MARKER).write_text(
        json.dumps(
            {
                "adapter_version": ADAPTER_VERSION,
                "task_id": task["task_id"],
                "source": task.get("source"),
                "source_task_id": task.get("source_task_id"),
                "repo": task.get("repo"),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return dest


def materialize_manifest(
    manifest: dict,
    tasks_root: Path,
    *,
    force: bool = False,
) -> list[Path]:
    """Materialize every task in a manifest; returns created directories."""
    created = []
    for task in manifest.get("tasks", []):
        created.append(materialize_task(task, tasks_root, force=force))
    return created
