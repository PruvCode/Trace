"""Shared service policy: single source of truth for token rules."""

TOKEN_PREFIX = "tok_"
TOKEN_MIN_LENGTH = 12
TOKEN_TTL_SECONDS = 3600


def is_within_ttl(elapsed_seconds):
    """Return True if a session of the given age is within the policy TTL."""
    return elapsed_seconds < TOKEN_TTL_SECONDS
