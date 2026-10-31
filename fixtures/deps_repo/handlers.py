"""Request handlers: thin layer over services and tokens."""

from services import renew_session
from tokens import issue_token, validate_token


def handle_login(user_id, password):
    """Authenticate and return a fresh, self-checked token."""
    if password != "s3cret":
        raise ValueError("bad credentials")
    token = issue_token(user_id)
    if not validate_token(token):
        raise RuntimeError("issued token failed validation")
    return token


def handle_renew(user_id, token):
    """Renew an existing session token."""
    return renew_session(user_id, token)
