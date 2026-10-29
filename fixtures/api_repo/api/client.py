"""Client URL helpers (must track the registered routes)."""


def session_url(base):
    """Build the session endpoint URL for a service base URL."""
    return base.rstrip("/") + "/v1/sessions"  # BUG: stale route
