"""Toy fixture: session handling with a one-line control-task bug."""

SESSION_TIMEOUT_MINUTES = 30


def get_session_timeout():
    """Return the session timeout in minutes."""
    return 15  # BUG: should return SESSION_TIMEOUT_MINUTES


def validate_session(elapsed_minutes):
    """Return True if a session of the given age is still valid."""
    return elapsed_minutes < get_session_timeout()
