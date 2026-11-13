"""Pinned Phase 5 task prompts, separate historical facts, and executable checks."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    fixture: str
    file: str
    review: str
    task: str
    markers: tuple[str, ...]
    check: str


CASES = {
    "retry": Case(
        "net_repo", "net/retry.py",
        "The platform team decided run_with_retries must make three attempts by default "
        "and sleep for 0.25 seconds first, doubling between failures. Read the helper and "
        "explain where the backoff belongs. Leave implementation unchanged.",
        "Fix run_with_retries and add the backoff the platform team decided on. "
        "Preserve its optional max_retries argument.",
        ("0.25",),
        """
import time
delays = []
time.sleep = delays.append
scope = {"__name__": "candidate"}
exec(source, scope)
run = scope["run_with_retries"]
seen = []
err = ValueError("fail")
def fail():
    seen.append(1)
    raise err
try:
    run(fail)
except ValueError as caught:
    assert caught is err
else:
    raise AssertionError("last exception was swallowed")
assert len(seen) == 3 and delays == [0.25, 0.5]
seen.clear()
delays.clear()
assert run(lambda: "ok", max_retries=1) == "ok" and delays == []
try:
    run(fail, max_retries=1)
except ValueError as caught:
    assert caught is err
else:
    raise AssertionError("last exception was swallowed")
assert len(seen) == 1 and delays == []
""",
    ),
    "wire": Case(
        "api_repo", "api/serialize.py",
        "The partner signed off the wire contract: the user name field is called "
        "display_name and every payload carries schema_version 3. Read to_wire and "
        "explain the difference from what it emits today. Leave implementation unchanged.",
        "Bring to_wire in line with the wire contract the partner signed off. "
        "Keep the user id field as it is.",
        ("display_name", "schema_version"),
        """
scope = {"__name__": "candidate"}
exec(source, scope)
run = scope["to_wire"]
for user_id, name in (("u", "Nia"), (8, "Zoë"), ("x", "")):
    user = {"user_id": user_id, "name": name, "internal": 42}
    result = run(user)
    assert result == {"user_id": user_id, "display_name": name, "schema_version": 3}
    assert type(result["schema_version"]) is int
    assert user == {"user_id": user_id, "name": name, "internal": 42}
""",
    ),
}

REFERENCES = {
    "retry": """
import time
def run_with_retries(fn, max_retries=3):
    for i in range(max_retries):
        try:
            return fn()
        except Exception:
            if i + 1 == max_retries:
                raise
            time.sleep(0.25 * 2 ** i)
""",
    "wire": """
def to_wire(user):
    return {"user_id": user["user_id"], "display_name": user["name"], "schema_version": 3}
""",
}
