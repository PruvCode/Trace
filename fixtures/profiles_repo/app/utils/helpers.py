"""Shared formatting helpers for logs and telemetry."""


def sanitize(text):
    """Normalize whitespace in a log/telemetry line."""
    return " ".join((text or "").split())
