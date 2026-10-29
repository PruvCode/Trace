"""Token issuance (credential fixture)."""


def refresh_token(user_id):
    """Issue a token for a user."""
    return f"token-{user_id}-fresh"


def validate_token(token):
    """Return True for freshly issued tokens."""
    return (
        isinstance(token, str)
        and token.startswith("token-")
        and token.endswith("-fresh")
    )
