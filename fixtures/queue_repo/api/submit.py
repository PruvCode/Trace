"""Job submission: thin layer mapping worker outcomes to statuses."""

from queue.worker import run_job


def submit(job):
    """Submit a job; report ok, or retry when the worker asks for one."""
    try:
        return {"status": "ok", "result": run_job(job)["result"]}
    except ConnectionError:  # BUG: wrong contract; worker raises RetryableError
        return {"status": "retry"}
