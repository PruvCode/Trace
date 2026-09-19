"""Unit tests: arg hashing stability + timer sanity."""

import time

from agent.tools import hash_args
from benchmark.metrics import RunTimer


def test_hash_args_stable_and_sensitive():
    assert hash_args({"b": 1, "a": 2}) == hash_args({"a": 2, "b": 1})
    assert hash_args({"path": "a"}) != hash_args({"path": "b"})


def test_run_timer_elapsed():
    timer = RunTimer()
    time.sleep(0.01)
    assert timer.elapsed() >= 0.01
