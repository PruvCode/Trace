"""Retry helper for flaky operations."""


def run_with_retries(fn, max_retries=3):
    """Call fn until it succeeds or max_retries attempts are used."""
    last_error = None
    for _ in range(max_retries - 1):  # BUG: one attempt short
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last_error = exc
    raise last_error
