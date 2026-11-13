"""Measure bounded recall against large, entirely synthetic project histories."""

import argparse
import json
import statistics
import tempfile
from contextlib import closing
from pathlib import Path
from time import perf_counter

from memory import episodic, session_context, store, synthetic, task_library, transcript

SIZES = (1000, 10000, 50000)
REPEATS = 20


def seed(database, messages):
    with closing(store.connect(database)) as conn:
        store.init_schema(conn)
        target = task_library.create_task(conn, "Ledger review")["id"]
        unrelated = task_library.create_task(conn, "Other ledger")["id"]
        for session, scope in (("now", target), ("prior", target), ("other", unrelated)):
            transcript.record_message(conn, session_id=session, role="system", content="Session opened",
                                      source_id=f"session:{session}")
            task_library.bind_session(conn, scope, session)
        with conn:
            for session, code in (("prior", "approved bluefin window is 23 minutes"),
                                  ("other", "WRONGPROJECTCODE"),
                                  ("loose", "approved amber window is 41 minutes")):
                transcript.record_message(
                    conn, autocommit=False, session_id=session, role="user",
                    content=f"Ledger review validation: {code}.",
                    timestamp="2026-01-01T00:00:00Z",
                )
            for number in range(messages - 6):
                session = "prior" if number % 5 == 0 else (
                    "other" if number % 5 == 1 else f"background-{number // 20}")
                text = "Ledger review validation pending" if session != "other" else "WRONGPROJECTCODE bluefin ledger"
                transcript.record_message(
                    conn, autocommit=False, session_id=session,
                    role="assistant" if number % 2 == 0 else "user",
                    content=f"{text}; routine update {number}.",
                    timestamp="2026-01-02T00:00:00Z",
                )
            for number in range(messages // 5):
                scope = target if number % 5 == 0 else unrelated if number % 5 == 1 else None
                code = "WRONGPROJECTCODE" if scope == unrelated else "routine observation"
                episodic.record_event(conn, autocommit=False, type="observation", repo="synthetic",
                                      task_id=scope, payload={"message": f"Ledger review {code} {number}."})
    return {"messages": messages, "events": messages // 5, "database_bytes": database.stat().st_size}


def measure(database, session, query, expected):
    times, outcomes = [], []
    for _ in range(REPEATS + 1):
        started = perf_counter()
        result = session_context.get_session_start_context(database, exclude_session_id=session, query=query)
        block = synthetic.render_user_block(result["items"])
        times.append((perf_counter() - started) * 1000)
        assert "WRONGPROJECTCODE" not in block
        assert 0 < len(block) <= session_context.MAX_TASK_CHARS
        outcomes.append((expected in block, len(block)))
    assert len(set(outcomes)) == 1
    found, size = outcomes[0]
    assert found
    warm = times[1:]
    return {"target_recalled": found, "block_chars": size, "first_ms": round(times[0], 2),
            "median_ms": round(statistics.median(warm), 2), "p95_ms": round(sorted(warm)[18], 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run with assertions enabled")
    with tempfile.TemporaryDirectory(prefix="trace-preview-scale-", dir=args.scratch) as path:
        for messages in SIZES:
            database = Path(path) / f"{messages}.db"
            summary = seed(database, messages)
            for label, session, term, answer in (("task", "now", "bluefin", "23 minutes"),
                                                 ("unassigned", "new", "amber", "41 minutes")):
                summary[label] = {
                    "selective": measure(database, session, f"{term} validation", answer),
                    "broad": measure(database, session, "ledger review validation", answer),
                }
            print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
