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
from memory import task_library

GIT_TIMEOUT = 60
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class FileChange:
    path: str
    added: int | None  # None for binary files
    deleted: int | None


def _git(repo: Path, *args: str) -> str:
    try:
        return task_library.local_git(repo, *args, timeout=GIT_TIMEOUT).decode("utf-8", errors="replace")
    except ValueError as exc:
        raise RuntimeError(f"local Git {args[0]} failed") from exc


def list_commits(repo: Path, path: str | None = None, max_commits: int = 50) -> list[str]:
    """SHAs newest-first, optionally restricted to a path. Bounded."""
    try:
        task_library.local_git(repo, "rev-parse", "--verify", "--quiet", "HEAD")
    except ValueError as exc:
        if isinstance(exc.__cause__, subprocess.CalledProcessError) and exc.__cause__.returncode == 1:
            return []
        raise RuntimeError("local Git could not resolve HEAD") from exc
    args = ["--literal-pathspecs", "rev-list", "--topo-order", "-n", str(max(1, max_commits)), "HEAD"]
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
    out = _git(repo, "diff-tree", "--numstat", "-z", "-r", "-m", "--first-parent",
               "--no-renames", "--no-commit-id", "--root", sha)
    changes: list[FileChange] = []
    for line in out.split("\0"):
        if not line.strip():
            continue
        parts = line.split("\t", 2)
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
    try:
        return task_library.local_git(repo, "show", f"{sha}:{path}", timeout=GIT_TIMEOUT)
    except ValueError:
        return None


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
    diff = _git(repo, "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv",
                "-U0", "--no-color", f"{sha}^", sha, "--", path)
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
    scope = task_library.git_scope(repo)
    if scope is None:
        raise ValueError("not a git repository or worktree")
    pathspec = scope.object_path(path.replace("\\", "/")) if path is not None else scope.prefix
    entries: list[dict] = []
    for sha in list_commits(scope.root, pathspec, max_commits):
        changes = commit_files(scope.root, sha)
        files = [(change, name) for change in changes
                 if (name := scope.project_path(change.path)) is not None]
        if scope.prefix and not files:
            continue
        if len(files) != len(changes):
            subject, body = "Commit also changes paths outside this project", ""
        else:
            subject, body = commit_message(scope.root, sha)
        attributed: set[str] = set()
        for change, _ in files:
            attributed.update(changed_symbols(scope.root, sha, change.path))
        symbols = sorted(attributed)
        if symbol is not None and symbol not in symbols:
            continue
        entries.append(
            {
                "sha": sha,
                "subject": subject,
                "message": body,
                "files": [
                    {"path": name, "added": f.added, "deleted": f.deleted}
                    for f, name in files
                ],
                "changed_symbols": symbols,
            }
        )
        if len(entries) >= store_mod.clamp_limit(limit):
            break
    return entries


def scoped_log(scope: task_library.GitScope, limit: int = 5) -> list[str]:
    """Summaries for a nested project without mixed-commit descriptions."""
    lines = []
    for sha in list_commits(scope.root, scope.prefix, limit):
        files = commit_files(scope.root, sha)
        if not any(scope.project_path(change.path) is not None for change in files):
            continue
        subject = ("Commit also changes paths outside this project"
                   if any(scope.project_path(change.path) is None for change in files)
                   else commit_message(scope.root, sha)[0])
        lines.append(f"{sha[:7]} {subject}")
    return lines
