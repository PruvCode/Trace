"""Deterministically seed fixtures/history_repo (3-commit linear history).

Same pattern as seed_toy_repo.py: tracked source is NOT committed here —
this script writes the files per commit and creates the git history with
fixed identity/dates, so SHAs are stable. The generated .git is gitignored.
Commit messages deliberately contain '|' and quotes to prove delimiter-safe
parsing (Phase 3 amendment 1).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "history_repo"

SEED_ENV = {
    "GIT_AUTHOR_NAME": "TRACE Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@localhost",
    "GIT_COMMITTER_NAME": "TRACE Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@localhost",
    "GIT_AUTHOR_DATE": "2026-02-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-02-01T00:00:00+00:00",
}

BASE_GIT_ARGS = [
    "-c",
    "core.fileMode=false",
    "-c",
    "core.autocrlf=false",
]

TOKENS_V1 = '''"""Token issuance (history fixture v1)."""


def refresh_token(user_id):
    """Issue a token for a user."""
    return f"token-{user_id}"


def validate_token(token):
    """Return True for issued tokens."""
    return token.startswith("token-")
'''

TOKENS_V2 = TOKENS_V1.replace(
    'return f"token-{user_id}"', 'return f"token-{user_id}-fresh"'
)

LOGIN_PY = '''"""Login flow (history fixture)."""

from auth.tokens import refresh_token


def login_and_refresh(user_id, password):
    """Authenticate, then refresh the session token."""
    if password != "s3cret":
        raise ValueError("bad credentials")
    return refresh_token(user_id)
'''

RENEWAL_PY = '''"""Session renewal (history fixture)."""

from auth.tokens import refresh_token


def renew_session(user_id):
    """Renew a session by refreshing its token."""
    return refresh_token(user_id)
'''


def _git(*args: str, cwd: Path) -> str:
    import os

    env = dict(os.environ)
    env.update(SEED_ENV)
    proc = subprocess.run(
        ["git", *BASE_GIT_ARGS, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _write(fixture_dir: Path, rel: str, content: str) -> None:
    path = fixture_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def ensure_seeded_history(fixture_dir: Path = FIXTURE_DIR) -> list[str]:
    """Create the 3-commit history if needed; return SHAs oldest-first."""
    git_dir = fixture_dir / ".git"
    if not git_dir.exists():
        fixture_dir.mkdir(parents=True, exist_ok=True)
        _git("init", "-b", "main", cwd=fixture_dir)
        _git("config", "core.fileMode", "false", cwd=fixture_dir)
        _git("config", "core.autocrlf", "false", cwd=fixture_dir)

        _write(fixture_dir, "auth/__init__.py", "")
        _write(fixture_dir, "auth/tokens.py", TOKENS_V1)
        _git("add", "-A", cwd=fixture_dir)
        _git("commit", "-m", "add token module", cwd=fixture_dir)

        _write(fixture_dir, "auth/tokens.py", TOKENS_V2)
        _write(fixture_dir, "auth/login.py", LOGIN_PY)
        _git("add", "-A", cwd=fixture_dir)
        _git(
            "commit",
            "-m",
            'call refresh_token from login | wire-up "v2"',
            "-m",
            "Wires login_and_refresh to refresh_token.\nSecond body line.",
            cwd=fixture_dir,
        )

        _write(fixture_dir, "services/__init__.py", "")
        _write(fixture_dir, "services/renewal.py", RENEWAL_PY)
        _git("add", "-A", cwd=fixture_dir)
        _git("commit", "-m", "renew sessions via refresh_token", cwd=fixture_dir)

    status = _git("status", "--porcelain", cwd=fixture_dir)
    if status:
        raise RuntimeError(f"history fixture has uncommitted changes:\n{status}")
    shas = _git("rev-list", "--topo-order", "HEAD", cwd=fixture_dir).splitlines()
    return list(reversed(shas))  # oldest first


def main() -> int:
    for sha in ensure_seeded_history():
        print(sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
