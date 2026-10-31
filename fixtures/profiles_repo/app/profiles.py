"""Public profile rendering."""

from app.utils.helpers import sanitize


def display_name(raw):
    """Render a raw profile name for display."""
    return sanitize(raw)
