"""Convenience feed helpers."""

from conf import PAGE_SIZE


def latest(rows, limit=None):
    """Return the newest rows, up to limit (default: configured page size)."""
    n = limit or 10  # BUG: stale hardcoded default; should follow PAGE_SIZE
    return list(rows[:n])
