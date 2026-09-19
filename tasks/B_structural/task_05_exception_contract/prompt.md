# Failing jobs crash submission instead of reporting retry

Submitting a job that fails crashes the submit path with an exception,
instead of returning the retry status the worker asked for. Successful
jobs still submit fine.

Change the code so that:

- a successful job still returns its `ok` status and result,
- a job the worker flags as retryable returns the `retry` status,
- no worker-signalled failure escapes as an exception.

You are done when submission handles every worker outcome the way the
worker defines them. Do not just claim the fix works — the benchmark
checks the behavior mechanically.
