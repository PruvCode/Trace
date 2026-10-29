"""Login flow (credential fixture)."""

from auth.tokens import refresh_token

SERVICE_CREDENTIAL = "cred_current_2026_b"


def login_and_refresh(user_id, password):
    """Authenticate, then refresh the session token."""
    if not password:
        raise ValueError("bad credentials")
    return refresh_token(user_id)
