"""Background job execution."""


class RetryableError(Exception):
    """A job failure worth retrying."""


def run_job(job):
    """Execute one job dict; return its result mapping."""
    if job.get("fail"):
        raise RetryableError("job failed")
    return {"result": job.get("payload")}
