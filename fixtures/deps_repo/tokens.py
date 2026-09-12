"""Token issuance and validation (canonical consumer of policy)."""

from policy import TOKEN_MIN_LENGTH, TOKEN_PREFIX


def issue_token(user_id):
    """Issue a fresh token for a user."""
    if not user_id:
        raise ValueError("unknown user")
    return f"{TOKEN_PREFIX}{user_id}_fresh"


def validate_token(token):
    """Return True only for tokens matching the shared policy."""
    return (
        isinstance(token, str)
        and token.startswith(TOKEN_PREFIX)
        and len(token) >= TOKEN_MIN_LENGTH
    )
