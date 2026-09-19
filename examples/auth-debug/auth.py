"""Minimal example project: session timeout bug in an auth module."""


SESSION_TIMEOUT_MINUTES = 30


def get_session_timeout():
    return SESSION_TIMEOUT_MINUTES


def refresh_token(user):
    if not user:
        raise ValueError("empty user")
    return f"token-{user}-{get_session_timeout()}"
