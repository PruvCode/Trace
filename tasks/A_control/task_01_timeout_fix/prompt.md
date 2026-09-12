# Fix the session timeout

In `auth/session.py`, `get_session_timeout()` returns a hard-coded wrong value
instead of the `SESSION_TIMEOUT_MINUTES` constant.

Change the code so that:

- `get_session_timeout()` returns `SESSION_TIMEOUT_MINUTES` (currently 30),
- `validate_session()` behavior follows from that fix (no separate change needed).

You are done when the test suite passes:

```text
python -m pytest tests/test_session.py -q
```

Do not just claim the fix works — the benchmark runs the tests mechanically.
