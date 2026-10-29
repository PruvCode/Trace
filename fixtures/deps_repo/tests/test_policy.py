"""Evaluator tests for the canonical session TTL (B2)."""

from policy import TOKEN_TTL_SECONDS
from services import session_is_valid


def test_ttl_matches_policy():
    assert TOKEN_TTL_SECONDS == 3600
    assert session_is_valid(3599) is True
    assert session_is_valid(3600) is False
