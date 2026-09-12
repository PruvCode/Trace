"""Shared fixtures: repo root + deterministically seeded toy fixture (session)."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.seed_toy_repo import ensure_seeded_fixture  # noqa: E402


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def seeded_fixture(repo_root: Path):
    fixture_dir = repo_root / "fixtures" / "toy_repo"
    head = ensure_seeded_fixture(fixture_dir)
    return fixture_dir, head
