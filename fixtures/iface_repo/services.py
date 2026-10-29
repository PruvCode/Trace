"""Session services: renewal entry point (interface fixture)."""

from tokens import validate_token


def renew_session(user_id, token):
    """Renew a session, rejecting malformed tokens."""
    if not validate_token(token):
        raise ValueError("bad token")
    return f"renewed-{user_id}-{token}"
