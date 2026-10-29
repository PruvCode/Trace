"""Login flow for the toy fixture."""

from auth.tokens import refresh_token


def login_and_refresh(user_id, password):
    """Authenticate, then refresh and return the session token."""
    if password != "s3cret":
        raise ValueError("bad credentials")
    return refresh_token(user_id)
