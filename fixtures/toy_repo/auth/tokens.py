"""Token issuance for the toy fixture."""


def refresh_token(user_id):
    """Issue a fresh token for a user."""
    return f"token-{user_id}-fresh"


def validate_token(token):
    """Return True if the token looks freshly issued."""
    return isinstance(token, str) and token.endswith("-fresh")
