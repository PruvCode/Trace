"""Git-derived history tests on the deterministic history_repo (Phase 3.2).

History (oldest-first): c1 "add token module", c2 pipe-and-quotes subject
with multi-line body, c3 "renew sessions via refresh_token".
"""

from memory import git_events as git_mod


def test_commit_order_and_subjects(seeded_history):
    repo, shas = seeded_history
    assert len(shas) == 3
    entries = git_mod.get_git_history(repo)
    assert [e["sha"] for e in entries] == list(reversed(shas))  # newest first
    assert entries[0]["subject"] == "renew sessions via refresh_token"
    assert entries[2]["subject"] == "add token module"


def test_pipe_and_body_survive_parsing(seeded_history):
    """Amendment 1: '|' and newlines in messages must parse exactly."""
    repo, shas = seeded_history
    entries = git_mod.get_git_history(repo)
    middle = entries[1]
    assert middle["sha"] == shas[1]
    assert middle["subject"] == 'call refresh_token from login | wire-up "v2"'
    assert middle["message"] == (
        "Wires login_and_refresh to refresh_token.\nSecond body line."
    )
    assert git_mod.get_git_history(repo)[2]["message"] == ""


def test_changed_files_and_line_counts(seeded_history):
    repo, shas = seeded_history
    entries = {e["sha"]: e for e in git_mod.get_git_history(repo)}
    c1 = entries[shas[0]]
    assert {f["path"] for f in c1["files"]} == {
        "auth/__init__.py",
        "auth/tokens.py",
    }
    tokens = next(f for f in c1["files"] if f["path"] == "auth/tokens.py")
    expected_lines = len(
        (repo / "auth" / "tokens.py").read_text(encoding="utf-8").splitlines()
    )
    assert (tokens["added"], tokens["deleted"]) == (expected_lines, 0)

    c2 = entries[shas[1]]
    assert {f["path"] for f in c2["files"]} == {
        "auth/tokens.py",
        "auth/login.py",
    }
    changed = next(f for f in c2["files"] if f["path"] == "auth/tokens.py")
    assert (changed["added"], changed["deleted"]) == (1, 1)  # one-line body fix


def test_changed_symbols_attribution(seeded_history):
    """Amendment 2: attribution is definitions-overlapping-hunks, verified on
    fixture behavior — call-only files attribute nothing for that name."""
    repo, shas = seeded_history
    entries = {e["sha"]: e for e in git_mod.get_git_history(repo)}
    assert "refresh_token" in entries[shas[0]]["changed_symbols"]  # new defs
    assert {"refresh_token", "login_and_refresh"} <= set(
        entries[shas[1]]["changed_symbols"]
    )
    c3_symbols = entries[shas[2]]["changed_symbols"]
    assert c3_symbols == ["renew_session"]  # refresh_token only CALLED there
    assert "refresh_token" not in c3_symbols


def test_path_and_symbol_filters(seeded_history):
    repo, shas = seeded_history
    only_renewal = git_mod.get_git_history(repo, path="services/renewal.py")
    assert [e["sha"] for e in only_renewal] == [shas[2]]
    with_symbol = git_mod.get_git_history(repo, symbol="refresh_token")
    assert [e["sha"] for e in with_symbol] == [shas[1], shas[0]]  # newest first
    assert git_mod.get_git_history(repo, path="no/such/file.py") == []
    assert git_mod.get_git_history(repo, symbol="no_such_symbol") == []


def test_limit_bounded_and_deterministic(seeded_history):
    repo, _shas = seeded_history
    assert len(git_mod.get_git_history(repo, limit=1)) == 1
    first = git_mod.get_git_history(repo)
    second = git_mod.get_git_history(repo)
    assert first == second
