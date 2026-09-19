"""Derive real C (episodic) and D (staleness) memory setups from git history.

Experimental-integrity rule: the corpus is selected from *facts in the
cached clone*, never from invented scenarios and never from experiment
results. This module is the only place that turns commit history into a
seeded-memory setup, so the provenance of every seeded event is auditable.

Definitions (mirroring the task brief):

C_episodic  a real prior investigation/attempt/decision about the same code
            the task touches, recorded BEFORE ``base_commit``. The memory is
            a genuine earlier fact, useful but not a substitute for reading
            current code.

D_staleness a real earlier state of a symbol/file that a LATER commit
            contradicted before ``base_commit``. Memory truthfully records
            the old state; the repository has since moved on, so an agent
            that blindly trusts it derives the wrong answer.

Both are derived with ``memory.git_events`` (Git facts, source="git") so the
seeded events reuse the existing episodic model and never invent payloads
that no commit supports.

Honesty rules honoured here:
- No commit -> no setup. A candidate without the required evidence simply
  does not qualify; it is never forced into C or D.
- The reused commits must be ancestors of ``base_commit`` (an earlier fact),
  never descendants (which would be leakage of the fix itself).
- ``staleness_setup`` records both the stale (old) and current (new) SHAs so
  a downstream report can state exactly what changed.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from memory import git_events as git_events_mod

GIT_TIMEOUT = 120

# Commit subjects that indicate a reverted/rejected change. Real evidence of
# a prior attempt; deliberately narrow to avoid false positives.
_REVERT_RE = re.compile(
    r"\b(revert|reverted|roll ?back|rolled ?back|undo|undid|back out)\b",
    re.IGNORECASE,
)

# A "fix" style subject: candidate prior investigations for C.
_FIX_RE = re.compile(
    r"\b(fix|fixed|fixes|bugfix|patch|correct|corrected|handle|prevent)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CandidateHistory:
    """Evidence gathered for one candidate's base commit."""

    sha: str
    subject: str
    # Real revert commits reachable from ancestors of base_commit.
    reverts: list[dict] = field(default_factory=list)
    # Real fix commits touching the same paths (prior investigations).
    fixes: list[dict] = field(default_factory=list)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant`` (or equal)."""
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )
    return proc.returncode == 0


def _subject(repo: Path, sha: str) -> str:
    """Commit subject; empty string when the object is unavailable locally.

    A subject is needed only for human-readable provenance, so an unavailable
    object degrades to no subject rather than aborting the whole derivation.
    """
    try:
        return _git(repo, "log", "-1", "--format=%s", sha).strip()
    except Exception:  # noqa: BLE001 - partial clone / network miss
        return ""


def _commit_epoch(repo: Path, sha: str) -> str:
    """Author date as ISO-8601 (UTC) — deterministic, no wall clock."""
    try:
        raw = _git(repo, "show", "-s", "--format=%aI", sha).strip()
    except Exception:  # noqa: BLE001 - partial clone / network miss
        raw = ""
    return raw or "1970-01-01T00:00:00+00:00"


def _touching_paths(repo: Path, sha: str, paths: list[str]) -> list[str]:
    """Intersection of a commit's changed files with ``paths`` (best-effort).

    ``commit_files`` can fail when the blobless clone must reach the promisor
    remote for an object it does not yet have (network blip, or offline). A
    missing object must never abort derivation: we return an empty
    intersection and the caller simply sees weaker evidence.
    """
    if not paths:
        return []
    try:
        changed = {
            change.path
            for change in git_events_mod.commit_files(repo, sha)
        }
    except Exception:  # noqa: BLE001 - network/partial-clone miss -> no evidence
        return []
    return sorted(p for p in paths if p in changed)


def _candidate_paths(candidate: dict, gold_files: list[str]) -> list[str]:
    """Paths a task's gold patch touches (from the manifest classification)."""
    return sorted(set(gold_files))


def find_revert_commits(
    repo: Path, base_commit: str, paths: list[str], *, max_commits: int = 400
) -> list[dict]:
    """Real revert commits among ``base_commit`` ancestors touching ``paths``.

    Bounded scan (``max_commits``) so a huge history cannot make validation
    unbounded. Newest-first; deterministic. Metadata-only: commit subjects and
    dates are read from the commit graph, so a blobless partial clone needs no
    network fetch (blob access is what makes this slow/flaky).
    """
    if not paths:
        return []
    return _scan_subjects(repo, base_commit, paths, _REVERT_RE, max_commits)


def find_fix_commits(
    repo: Path, base_commit: str, paths: list[str], *, max_commits: int = 400
) -> list[dict]:
    """Real fix-style commits among ``base_commit`` ancestors touching paths."""
    if not paths:
        return []
    found = _scan_subjects(repo, base_commit, paths, _FIX_RE, max_commits)
    return [entry for entry in found if not _REVERT_RE.search(entry["subject"])]


def _scan_subjects(
    repo: Path,
    base_commit: str,
    paths: list[str],
    pattern: re.Pattern,
    max_commits: int,
) -> list[dict]:
    """One pass over ``git log`` with a pathspec; classify subjects in memory.

    Using ``git log --format`` (single invocation, metadata only) keeps this
    fast on a blobless clone: no per-commit subprocess and no blob fetch.
    """
    out = _git(
        repo,
        "log",
        "--topo-order",
        "-n",
        str(max(1, max_commits)),
        "--format=%H%x1f%s%x1f%aI",
        base_commit,
        "--",
        *paths,
    )
    results: list[dict] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, subject, timestamp = parts
        if not pattern.search(subject):
            continue
        results.append(
            {
                "sha": sha,
                "subject": subject,
                "timestamp": timestamp or "1970-01-01T00:00:00+00:00",
                "touching_paths": list(paths),
            }
        )
    return results


def _symbols_touched(
    repo: Path, sha: str, paths: list[str], *, allow_blob_fetch: bool = False
) -> list[str]:
    """Definition names attributed to a commit within ``paths`` (bounded).

    Disabled by default: attribution needs blob contents, which a partial
    clone must fetch over the network (slow, flaky). Provenance does not
    depend on it, so a metadata-only derivation simply omits symbols.
    """
    if not allow_blob_fetch:
        return []
    symbols: set[str] = set()
    for path in paths:
        try:
            symbols.update(git_events_mod.changed_symbols(repo, sha, path))
        except Exception:  # noqa: BLE001 - unparseable blob attributes nothing
            continue
    return sorted(symbols)


def _file_state_at(repo: Path, sha: str, path: str) -> str | None:
    try:
        return _git(repo, "show", f"{sha}:{path}")
    except Exception:  # noqa: BLE001 - missing blob / network miss -> unknown
        return None


def derive_episodic_setup(
    repo: Path,
    base_commit: str,
    paths: list[str],
) -> dict | None:
    """Build a C_episodic memory setup from real ancestors of base_commit.

    Prefers a real revert (a prior attempt that was rolled back — strong,
    unambiguous episodic evidence); falls back to a prior fix commit on the
    same paths. Returns ``None`` when no real evidence exists (the candidate
    then does not qualify for C).
    """
    if not paths:
        return None
    reverts = find_revert_commits(repo, base_commit, paths)
    fixes = find_fix_commits(repo, base_commit, paths)
    if reverts:
        chosen = reverts[0]
        event_type = "attempt"
        note = (
            f"prior change touching {', '.join(chosen['touching_paths'])} was "
            f"reverted before this task's base commit"
        )
        confidence = "reverted_attempt"
    elif fixes:
        chosen = fixes[0]
        event_type = "investigation"
        note = (
            f"prior fix touching {', '.join(chosen['touching_paths'])} exists "
            f"before this task's base commit"
        )
        confidence = "prior_fix"
    else:
        return None

    symbols = _symbols_touched(repo, chosen["sha"], chosen["touching_paths"])
    return {
        "kind": "episodic",
        "confidence": confidence,
        "derived_from": {
            "sha": chosen["sha"],
            "subject": chosen["subject"],
            "timestamp": chosen["timestamp"],
            "touching_paths": chosen["touching_paths"],
            "is_ancestor_of_base": True,
            "base_commit": base_commit,
        },
        "events": [
            {
                "type": event_type,
                "source": "git",
                "timestamp": chosen["timestamp"],
                "repo": None,  # filled by caller with the fixture/repo id
                "commit": chosen["sha"],
                "file": (chosen["touching_paths"] or [None])[0],
                "symbol": (symbols or [None])[0],
                "payload": {
                    "provenance": "git_history",
                    "subject": chosen["subject"],
                    "note": note,
                    "changed_symbols": symbols,
                },
            }
        ],
    }

def derive_staleness_setup(
    repo: Path,
    base_commit: str,
    paths: list[str],
) -> dict | None:
    """Build a D_staleness setup: an old fact contradicted before base_commit.

    Uses the commit immediately preceding base_commit that touched ``paths``
    as the "earlier fact" and the base commit as the current truth. The two
    are genuinely different revisions of the same path, so memory truthfully
    records the older state — that is what makes it stale. Metadata-only: the
    superseding relationship is proven by ancestry, not by blob diffing.
    """
    if not paths:
        return None
    out = _git(
        repo,
        "rev-list",
        "--topo-order",
        "-n",
        "2",
        base_commit,
        "--",
        *paths,
    ).strip()
    shas = [s.strip() for s in out.splitlines() if s.strip()]
    if len(shas) < 2:
        return None
    current_sha, prior_sha = shas[0], shas[1]
    if not is_ancestor(repo, prior_sha, current_sha):
        return None

    prior_subject = _subject(repo, prior_sha)
    current_subject = _subject(repo, current_sha)
    if prior_subject == current_subject and prior_sha == current_sha:
        return None

    return {
        "kind": "staleness",
        "confidence": "superseded_before_base",
        "stale_sha": prior_sha,
        "current_sha": current_sha,
        "changed_paths": list(paths),
        "derived_from": {
            "stale_subject": prior_subject,
            "current_subject": current_subject,
            "is_stale_ancestor_of_current": True,
        },
        "events": [
            {
                "type": "observation",
                "source": "git",
                "timestamp": "1970-01-01T00:00:00+00:00",
                "repo": None,  # filled by caller
                "commit": prior_sha,
                "file": paths[0],
                "symbol": None,
                "payload": {
                    "provenance": "git_history",
                    "finding": (
                        f"as of {prior_sha[:10]} ({prior_subject!r}), "
                        f"{paths[0]} had a different implementation"
                    ),
                    "superseded_by": current_sha,
                    "stale_subject": prior_subject,
                },
            }
        ],
    }


def gather(
    repo: Path,
    base_commit: str,
    gold_files: list[str],
) -> CandidateHistory:
    """Evidence summary for one candidate (bounded, deterministic)."""
    paths = _candidate_paths({}, gold_files)
    return CandidateHistory(
        sha=base_commit,
        subject=_subject(repo, base_commit),
        reverts=find_revert_commits(repo, base_commit, paths),
        fixes=find_fix_commits(repo, base_commit, paths),
    )
