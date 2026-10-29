"""Session renewal (history fixture)."""

from auth.tokens import refresh_token


def renew_session(user_id):
    """Renew a session by refreshing its token."""
    return refresh_token(user_id)
