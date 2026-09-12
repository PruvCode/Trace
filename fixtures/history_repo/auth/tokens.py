"""Token issuance (history fixture v1)."""


def refresh_token(user_id):
    """Issue a token for a user."""
    return f"token-{user_id}-fresh"


def validate_token(token):
    """Return True for issued tokens."""
    return token.startswith("token-")
