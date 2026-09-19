"""Deterministic Git-derived history (Phase 3.2).

Ground-truth rule: these are Git FACTS (sha, message, files, line counts,
attributed symbols), never agent claims. Callers store them with
source="git" via memory.episodic, keeping them distinguishable.

Robustness (Phase 3 amendment 1): SHAs and messages are retrieved
separately — commit bodies are taken as whole stdout, never split on
'|' or newlines. NUL bytes cannot appear in git messages, but we do not
even rely on that: no in-band delimiters are parsed for messages at all.

Attribution (Phase 3 amendment 2): changed_symbols is BEST-EFFORT
deterministic attribution — definitions overlapping changed hunks — NOT
proof that a symbol semantically changed. A call-only file (e.g. one that
merely calls refresh_token) attributes no symbol for that name.

Python-only symbol extraction reuses memory.structural; binaries, deleted
files, and renames are handled explicitly (documented limits).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from memory import store as store_mod
from memory import structural as structural_mod

GIT_TIMEOUT = 60
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class FileChange:
    path: str
    added: int | None  # None for binary files
    deleted: int | None


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def list_commits(repo: Path, path: str | None = None, max_commits: int = 50) -> list[str]:
    """SHAs newest-first, optionally restricted to a path. Bounded."""
    args = ["rev-list", "--topo-order", "-n", str(max(1, max_commits)), "HEAD"]
    if path:
        args += ["--", path]
    out = _git(repo, *args).strip()
    return out.splitlines() if out else []


def commit_message(repo: Path, sha: str) -> tuple[str, str]:
    """(subject, body) for one SHA. Message taken whole; never delimited."""
    out = _git(repo, "log", "-1", "--format=%s%n%b", sha)
    lines = out.splitlines()
    subject = lines[0] if lines else ""
    # Body taken whole: strip only surrounding newlines, never split content.
    body = "\n".join(lines[1:]).strip("\n")
    return subject, body


def commit_files(repo: Path, sha: str) -> list[FileChange]:
    """Changed files with added/deleted line counts (--root for first commit)."""
    out = _git(repo, "diff-tree", "--numstat", "-r", "--no-commit-id", "--root", sha)
    changes: list[FileChange] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_raw, deleted_raw, path = parts
        changes.append(
            FileChange(
                path=path,
                added=None if added_raw == "-" else int(added_raw),
                deleted=None if deleted_raw == "-" else int(deleted_raw),
            )
        )
    return changes


def _show(repo: Path, sha: str, path: str) -> bytes | None:
    proc = subprocess.run(
        ["git", "show", f"{sha}:{path}"],
        cwd=str(repo),
        capture_output=True,
        timeout=GIT_TIMEOUT,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _new_side_ranges(diff_text: str) -> list[tuple[int, int]]:
    """Parse `@@ ... +start[,count] @@` headers into [start, end) line ranges."""
    ranges: list[tuple[int, int]] = []
    for line in diff_text.splitlines():
        match = HUNK_HEADER.match(line)
        if match:
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            ranges.append((start, start + count))
    return ranges


def changed_symbols(repo: Path, sha: str, path: str) -> list[str]:
    """Definitions whose span overlaps changed hunks in one file at one commit.

    Best-effort attribution: new files attribute all their definitions;
    deleted/missing blobs and non-Python files attribute none. Span overlap
    (not start-line coincidence) is used so body-only edits attribute their
    enclosing definition.
    """
    if not path.endswith(".py"):
        return []
    after = _show(repo, sha, path)
    if after is None:
        return []
    before = _show(repo, f"{sha}^", path)
    try:
        relpath = path
        symbols = structural_mod.extract_symbols(after, relpath)
    except Exception:  # noqa: BLE001 - unparseable blob attributes nothing
        return []
    if before is None:
        return sorted({s.name for s in symbols})
    diff = _git(repo, "diff", "-U0", "--no-color", f"{sha}^", sha, "--", path)
    ranges = _new_side_ranges(diff)
    if not ranges:
        return []
    return sorted(
        {
            s.name
            for s in symbols
            if any(s.line < end and s.end_line >= start for start, end in ranges)
        }
    )


def get_git_history(
    repo: Path,
    path: str | None = None,
    symbol: str | None = None,
    limit: int = 10,
    max_commits: int = 50,
) -> list[dict]:
    """Per-commit Git facts, newest-first, bounded. Symbol filter is
    post-attribution (a commit qualifies only if the symbol is attributed)."""
    entries: list[dict] = []
    for sha in list_commits(repo, path, max_commits):
        subject, body = commit_message(repo, sha)
        files = commit_files(repo, sha)
        attributed: set[str] = set()
        for change in files:
            attributed.update(changed_symbols(repo, sha, change.path))
        symbols = sorted(attributed)
        if symbol is not None and symbol not in symbols:
            continue
        entries.append(
            {
                "sha": sha,
                "subject": subject,
                "message": body,
                "files": [
                    {"path": f.path, "added": f.added, "deleted": f.deleted}
                    for f in files
                ],
                "changed_symbols": symbols,
            }
        )
        if len(entries) >= store_mod.clamp_limit(limit):
            break
    return entries
