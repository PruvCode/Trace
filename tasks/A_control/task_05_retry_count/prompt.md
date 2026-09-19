# Fix the retry count

`run_with_retries()` in `net/retry.py` gives up too early: with
`max_retries=3` an operation that fails twice and then succeeds is
never given its third attempt.

Change the code so that:

- the function attempts the operation up to `max_retries` times,
- it returns the first success,
- it re-raises the last error only after all attempts are used.

You are done when flaky operations get every attempt they were
promised. Do not just claim the fix works — the benchmark checks the
behavior mechanically.
