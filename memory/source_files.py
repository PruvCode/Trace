"""Safe, repository-relative inputs for structural indexing and code maps."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from memory.task_library import local_git

SKIP_DIRS = frozenset({
    ".git", ".hg", ".agent-memory", ".obsidian", ".venv", ".pytest_cache",
    ".hypothesis", "__pycache__", "node_modules", ".astro", ".next", "dist",
})
PRIVATE_SUFFIXES = frozenset({".key", ".pem", ".pfx", ".p12", ".sqlite", ".sqlite3", ".db"})
PRIVATE_NAMES = frozenset({".npmrc", ".pypirc", "credentials.json", "id_rsa", "id_ed25519"})
MAX_FILE_BYTES = 256_000


def _redirected(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def regular_file(root: Path, name: str) -> os.stat_result | None:
    parts = name.split("/")
    if (
        not name or len(name) > 1024 or "\\" in name or ":" in name
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name)
        or any(part in ("", ".", "..") or part.casefold() in SKIP_DIRS for part in parts)
        or any(part.casefold().startswith(".env") for part in parts)
        or parts[-1].casefold() in PRIVATE_NAMES
        or Path(parts[-1]).suffix.lower() in PRIVATE_SUFFIXES
    ):
        return None
    path = root.joinpath(*parts)
    try:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and not _redirected(info) and path.resolve() == path:
            return info
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def inventory(root: Path, source_suffixes: tuple[str, ...] | None = None) -> dict[str, os.stat_result]:
    """Use Git's visible files where available and prune ignored directories in plain folders."""
    if any((directory / ".git").exists() for directory in (root, *root.parents)):
        try:
            names = local_git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", ".")
        except ValueError as exc:
            raise ValueError("could not list the visible project files with local Git") from exc
        candidates = (name.decode("utf-8", "replace") for name in names.split(b"\0") if name)
    else:
        paths = []
        for directory, subdirs, files in os.walk(root):
            base = Path(directory)
            safe = []
            for child in subdirs:
                if child.casefold() in SKIP_DIRS or child.casefold().startswith(".env"):
                    continue
                try:
                    if not _redirected((base / child).lstat()):
                        safe.append(child)
                except OSError:
                    continue
            subdirs[:] = safe
            paths.extend((base / name).relative_to(root).as_posix() for name in files)
        candidates = iter(paths)
    if source_suffixes is not None:
        candidates = (name for name in candidates if name.lower().endswith(source_suffixes)
                      or name.rsplit("/", 1)[-1] == "package.json")
    return {name: info for name in sorted(set(candidates)) if (info := regular_file(root, name)) is not None}


def read_source(root: Path, name: str) -> bytes | None:
    info = regular_file(root, name)
    if info is None or info.st_size > MAX_FILE_BYTES:
        return None
    with (root / name).open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    return data if len(data) <= MAX_FILE_BYTES and b"\0" not in data else None
