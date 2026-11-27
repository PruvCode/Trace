from hashlib import sha256
import json
from zipfile import ZipFile

import pytest

from scripts import release_wheel


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    wheel = tmp_path / "trace-1.2.3-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("trace-1.2.3.dist-info/METADATA", "Name: trace\nVersion: 1.2.3\n")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v1.2.3")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/Trace")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    monkeypatch.setenv("TRACE_WHEEL_SHA256", sha256(wheel.read_bytes()).hexdigest())
    return wheel


def test_records_and_verifies_wheel(candidate):
    directory = candidate.parent
    expected = sha256(candidate.read_bytes()).hexdigest()

    release_wheel.record(directory)
    release_wheel.verify(directory)

    info = json.loads((directory / "build-info.json").read_text(encoding="utf-8"))
    assert info == {
        "filename": candidate.name,
        "version": "1.2.3",
        "sha256": expected,
        "commit": "a" * 40,
        "ref": "refs/tags/v1.2.3",
        "workflow_run": "https://github.com/example/Trace/actions/runs/42",
    }
    assert (directory / "outputs").read_text(encoding="utf-8") == f"sha256={expected}\nversion=1.2.3\n"


def test_rejects_tag_that_does_not_match_wheel(candidate, monkeypatch):
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v1.2.2")
    with pytest.raises(ValueError, match="tag v1.2.2 does not match wheel version 1.2.3"):
        release_wheel.record(candidate.parent)
    assert not (candidate.parent / "build-info.json").exists()


def test_rejects_wheel_changed_after_build(candidate):
    release_wheel.record(candidate.parent)
    with ZipFile(candidate, "a") as archive:
        archive.writestr("trace_memory/extra.py", "# made after this build")
    with pytest.raises(ValueError, match="wheel checksum mismatch"):
        release_wheel.verify(candidate.parent)
