"""Evaluator tests for renewal/canonical-validation agreement (B1)."""

from services import renew_session
from tokens import validate_token

BATTERY = [
    "tok_abcdefghij",
    "tok_123456789012345",
    "tok_x",
    "bad",
    "",
    None,
    123,
]


def test_renew_agrees_with_canonical_validation():
    for token in BATTERY:
        expected = validate_token(token)
        try:
            renew_session("u1", token)
            actual = True
        except ValueError:
            actual = False
        assert actual == expected, f"renew disagrees on {token!r}"
