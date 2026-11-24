"""The documented example and staging safeguards work through the local CLI."""

import json
import os
import sys
from pathlib import Path

from memory import source_files, structural
from memory.task_library import local_git
from research.phase6 import auth_debug_smoke
from trace_memory import cli, project


def test_auth_example(tmp_path):
    assert auth_debug_smoke.run(
        tmp_path / "checkout", Path(sys.executable), Path(__file__).resolve().parents[2]
    ) == {"decision_id": 2, "context_retrieved": True, "nested_git_report": True}


def test_install_and_doctor_flag_stageable_and_tracked_nested_memory(tmp_path, capsys):
    local_git(tmp_path, "init", "-q")
    child = tmp_path / "nested"
    child.mkdir()
    assert cli.main(["install", "--agent", "codex", "--project", str(child)]) == 0
    setup = json.loads(capsys.readouterr().out)
    assert "/nested/.agent-memory/" in setup["git_privacy"]["detail"]
    excludes = tmp_path / ".git/info/exclude"
    with excludes.open("a", encoding="utf-8") as handle:
        handle.write("\n/nested/.agent-memory/\n/nested/.trace-service/\n")
    assert project.gitignore_hint(child) is None
    local_git(tmp_path, "add", "-f", "--", "nested/.agent-memory/memory.db")
    assert cli.main(["doctor", "--agent", "codex", "--project", str(child)]) == 0
    assert "already tracked or staged" in json.loads(capsys.readouterr().out)["git_privacy"]["detail"]


def test_same_metadata_different_content_refreshes_symbols(tmp_path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("def alpha():\n    return 1\n", encoding="utf-8")
    project.init_project(tmp_path)
    original = source.stat()
    snapshot = source_files.inventory(tmp_path)
    source.write_text("def bravo():\n    return 1\n", encoding="utf-8")
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    monkeypatch.setattr(structural.source_files, "inventory", lambda _: snapshot)
    assert project.find_definitions(tmp_path, "alpha") == []
    assert project.find_definitions(tmp_path, "bravo")[0]["file"] == "app.py"


def test_project_commands_reject_a_shared_memory_file(tmp_path, capsys, monkeypatch):
    owner = tmp_path / "owner"
    other = tmp_path / "other"
    owner.mkdir()
    other.mkdir()
    project.init_project(owner)
    monkeypatch.chdir(owner)
    assert cli.main(["record", "--type", "decision",
                     "--message", "Private fixture architecture decision"]) == 0
    capsys.readouterr()
    original = project.db_path(owner)
    snapshot = original.read_bytes()
    borrowed = project.db_path(other)
    borrowed.parent.mkdir()
    os.link(original, borrowed)

    monkeypatch.chdir(other)
    for args in (["context", "architecture decision"], ["init"], ["find", "entrypoint"],
                 ["privacy"], ["backup", str(tmp_path / "borrowed.db")]):
        assert cli.main(args) == 2
        output = capsys.readouterr()
        assert "regular project-local files" in output.err
        assert "Private fixture" not in output.out
        assert original.read_bytes() == snapshot
    assert not (tmp_path / "borrowed.db").exists()
