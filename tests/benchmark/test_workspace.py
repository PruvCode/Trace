"""Workspace tests: pinned start state + real isolation between runs."""

import pytest

from benchmark import workspace as workspace_mod


def test_create_workspace_at_pinned_commit(tmp_path, seeded_fixture):
    fixture_dir, head = seeded_fixture
    dest = tmp_path / "ws1"
    workspace_mod.create_workspace(fixture_dir, head, dest)
    assert workspace_mod.workspace_head(dest) == head
    assert "return 15" in (dest / "auth" / "session.py").read_text(encoding="utf-8")


def test_workspaces_isolated(tmp_path, seeded_fixture):
    """Proves dirt in run A is invisible to run B and the fixture is untouched."""
    fixture_dir, head = seeded_fixture
    ws_a = workspace_mod.create_workspace(fixture_dir, head, tmp_path / "ws_a")
    ws_b = workspace_mod.create_workspace(fixture_dir, head, tmp_path / "ws_b")

    (ws_a / "MARKER_A.txt").write_text("run A was here", encoding="utf-8")
    session = ws_a / "auth" / "session.py"
    session.write_text(
        session.read_text(encoding="utf-8").replace("return 15", "return 999"),
        encoding="utf-8",
    )

    assert not (ws_b / "MARKER_A.txt").exists()
    assert "return 15" in (ws_b / "auth" / "session.py").read_text(encoding="utf-8")
    assert workspace_mod.workspace_head(ws_b) == head

    status_lines = workspace_mod.workspace_git_status(ws_a)
    assert any("MARKER_A" in line for line in status_lines)
    assert workspace_mod.workspace_git_status(ws_b) == []

    # Fixture repo itself untouched.
    assert workspace_mod.workspace_head(fixture_dir) == head
    assert workspace_mod.workspace_git_status(fixture_dir) == []


def test_base_commit_mismatch_rejected(tmp_path, seeded_fixture):
    fixture_dir, _head = seeded_fixture
    with pytest.raises(Exception, match="(?i)(checkout|rev-parse|failed)"):
        workspace_mod.create_workspace(
            fixture_dir, "0" * 40, tmp_path / "ws_bad"
        )


def test_dest_exists_rejected(tmp_path, seeded_fixture):
    fixture_dir, head = seeded_fixture
    dest = tmp_path / "ws_dup"
    dest.mkdir()
    with pytest.raises(FileExistsError):
        workspace_mod.create_workspace(fixture_dir, head, dest)
