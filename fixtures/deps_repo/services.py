"""Session services: renewal and expiry checks."""

SESSION_TTL_SECONDS = 1800


def renew_session(user_id, token):
    """Renew a session, rejecting malformed tokens."""
    if not isinstance(token, str) or not token.startswith("tok_"):
        raise ValueError("bad token")
    return token


def session_is_valid(elapsed_seconds):
    """Return True if a session of the given age is still valid."""
    return elapsed_seconds < SESSION_TTL_SECONDS
