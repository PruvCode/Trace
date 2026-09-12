"""Evaluator tests for the timeout control task (run inside the workspace)."""

from auth.session import SESSION_TIMEOUT_MINUTES, get_session_timeout, validate_session


def test_timeout_value():
    assert get_session_timeout() == 30


def test_timeout_matches_constant():
    assert get_session_timeout() == SESSION_TIMEOUT_MINUTES


def test_validate_session_boundary():
    assert validate_session(29) is True
    assert validate_session(30) is False
