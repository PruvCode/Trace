"""The test suite must never resolve the user's real OpenCode data.

Checks the paths TRACE actually computes at runtime (capture service,
plugin, setup) against the session's isolated XDG home, in both
deterministic and live (TRACE_LIVE_OPENCODE=1) modes.
"""

from __future__ import annotations

import os
from pathlib import Path

from memory import opencode_plugin as plugin_mod
from memory import opencode_service as svc_mod
from trace_memory import project as project_mod

REAL_OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def test_opencode_paths_resolve_inside_the_isolated_home(_isolated_opencode_home, tmp_path):
    root = Path(_isolated_opencode_home).resolve()
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        assert Path(os.environ[name]).resolve().is_relative_to(root), name

    # A stale PWD would make OpenCode run in the directory pytest started in.
    assert "PWD" not in os.environ

    resolved = svc_mod.default_opencode_db_path().resolve()
    assert resolved.is_relative_to(root)
    assert resolved != REAL_OPENCODE_DB.resolve()
    # Live mode: the real binary was pointed at the isolated home and created
    # its database there (proves OpenCode honors the override, not just TRACE).
    assert resolved.exists()

    project = tmp_path / "proj"
    project.mkdir()
    (project / "x.py").write_text("x = 1\n", encoding="utf-8")
    project_mod.init_project(project)
    assert svc_mod.get_service(project).opencode_db_path.resolve().is_relative_to(root)

    plugin_mod.install_plugin(project)
    source = plugin_mod.plugin_path(project).read_text(encoding="utf-8")
    baked = next(line for line in source.splitlines() if line.startswith("const OPENCODE_DB"))
    assert str(REAL_OPENCODE_DB.parent).replace("\\", "\\\\") not in baked
    assert str(root.name) in baked
