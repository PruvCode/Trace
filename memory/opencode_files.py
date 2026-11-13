"""File ownership checks shared by the OpenCode installer and teardown."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


INTEGRATION_DISABLED = "integration.disabled"


def integration_marker(project: Path) -> Path:
    from memory.opencode_service import SERVICE_DIR_NAME

    return project / SERVICE_DIR_NAME / INTEGRATION_DISABLED


def require_local_paths(project: Path, *paths: Path) -> None:
    """Reject links and junctions before touching files in a project."""
    from trace_memory.project import TraceUserError

    for path in paths:
        try:
            local = path.is_relative_to(project) and not path.is_symlink() and path.resolve() == path
        except (OSError, RuntimeError):
            local = False
        if not local:
            raise TraceUserError(
                f"refusing to modify {path}",
                "the integration path is redirected or outside the project.",
                "Use regular project-local files and directories for OpenCode integration.",
            )


def atomic_write_text(path: Path, text: str) -> None:
    """Replace a config or plugin without exposing partially written text."""
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".trace-", delete=False,
        ) as handle:
            temp = Path(handle.name)
            handle.write(text)
        if path.exists():
            shutil.copymode(path, temp)
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
