"""Login flow (history fixture)."""

from auth.tokens import refresh_token


def login_and_refresh(user_id, password):
    """Authenticate, then refresh the session token."""
    if password != "s3cret":
        raise ValueError("bad credentials")
    return refresh_token(user_id)
