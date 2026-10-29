"""Shared fixtures: repo root + deterministically seeded toy fixture (session)."""

import os
import sys
from pathlib import Path

import pytest

# Tests that drive the real `opencode` binary (network + model). Opt-in only,
# and when enabled they run against an isolated OpenCode data/config/cache
# directory so they can never read or write the user's real sessions.
LIVE_OPENCODE_FILES = {
    "test_real_opencode_blackbox.py",
    "test_441_environment.py",
    "test_end_to_end_transparent.py",
    "test_retrieval_blackbox.py",
}
# Individual tests elsewhere that launch the real `opencode run`.
LIVE_OPENCODE_TESTS = {
    "test_capture_opencode_session_records_transcript",
    "test_capture_session_yields_messages",
}
LIVE_OPENCODE = os.environ.get("TRACE_LIVE_OPENCODE") == "1"


def pytest_collection_modifyitems(config, items):
    if LIVE_OPENCODE:
        return
    skip = pytest.mark.skip(
        reason="real-OpenCode test: opt in with TRACE_LIVE_OPENCODE=1 "
        "(runs against an isolated OpenCode data directory)"
    )
    for item in items:
        if item.path.name in LIVE_OPENCODE_FILES or item.originalname in LIVE_OPENCODE_TESTS:
            item.add_marker(skip)


_FAKE_OPENCODE_SCHEMA = """
CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT NOT NULL, parent_id TEXT,
    title TEXT, slug TEXT, model TEXT, agent TEXT,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
"""


@pytest.fixture(scope="session", autouse=True)
def _isolated_opencode_home(tmp_path_factory):
    """Every test session gets a throwaway XDG home for OpenCode.

    Nothing in the suite can read or write the user's real OpenCode data,
    even by accident. Deterministic runs get an empty OpenCode database with
    the schema capture reads; live runs let the real binary create its own.
    """
    import sqlite3

    root = tmp_path_factory.mktemp("opencode-home")
    names = ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")
    saved = {name: os.environ.get(name) for name in (*names, "PWD")}
    for name in names:
        path = root / name.split("_")[1].lower()
        path.mkdir()
        os.environ[name] = str(path)
    # OpenCode on Windows takes its project directory from an inherited PWD
    # (Git Bash exports one), not the subprocess cwd. A stale PWD would run
    # live tests in whatever directory pytest was launched from.
    os.environ.pop("PWD", None)
    if not LIVE_OPENCODE:
        db = root / "data" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.executescript(_FAKE_OPENCODE_SCHEMA)
        conn.close()
    else:
        # A fresh isolated home has no OpenCode database until OpenCode first
        # runs; live tests (and TRACE setup) expect one. Let the real binary
        # create it inside the isolated home.
        import shutil
        import subprocess

        # The installed `opencode` may be newer than the version TRACE's
        # automatic path was validated with (1.18.x); tests can point at a
        # version-pinned disposable install via TRACE_OPENCODE_BIN.
        binary = os.environ.get("TRACE_OPENCODE_BIN") or shutil.which("opencode")
        if binary:
            subprocess.run([binary, "models"], capture_output=True, timeout=300,
                           cwd=str(root), stdin=subprocess.DEVNULL)
    try:
        yield root
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.seed_deps_repo import ensure_seeded_deps  # noqa: E402
from scripts.seed_history_repo import ensure_seeded_history  # noqa: E402
from scripts.seed_toy_repo import ensure_seeded_fixture  # noqa: E402

@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def seeded_fixture(repo_root: Path):
    fixture_dir = repo_root / "fixtures" / "toy_repo"
    head = ensure_seeded_fixture(fixture_dir)
    return fixture_dir, head


@pytest.fixture(scope="session")
def seeded_history(repo_root: Path):
    history_dir = repo_root / "fixtures" / "history_repo"
    shas = ensure_seeded_history(history_dir)
    return history_dir, shas


@pytest.fixture(scope="session")
def seeded_deps(repo_root: Path):
    deps_dir = repo_root / "fixtures" / "deps_repo"
    head = ensure_seeded_deps(deps_dir)
    return deps_dir, head
