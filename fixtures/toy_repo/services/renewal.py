"""Session renewal for the toy fixture."""

from auth.tokens import refresh_token


def renew_session(user_id):
    """Renew an existing session by refreshing its token."""
    return refresh_token(user_id)
