"""Model-free retrieval benchmark: does the block carry the fact a new task needs?

Builds one deterministic project history (40 sessions, topic-sharing distractors)
and asks, per case: given the new task's prompt, is the needed fact in the block,
and how large is the block? `dev` cases were used for tuning. `held` cases must be
scored only after the code is frozen.

Usage: python research/phase3/retrieval_eval.py dev|held [--recency]
"""

from __future__ import annotations

import json
import random
import statistics
import sys
import tempfile
from pathlib import Path

from memory import session_context as sc
from memory import store, synthetic, transcript

AREAS = {
    "auth": ["login", "token", "session", "password reset", "refresh flow", "middleware"],
    "cache": ["cache key", "eviction", "user lookup", "TTL", "invalidation", "hit rate"],
    "paging": ["cursor", "page size", "offset", "feed endpoint", "next link", "sort order"],
    "data": ["migration", "backfill", "ingest job", "csv loader", "schema", "batch size"],
    "ci": ["flaky test", "pipeline", "coverage", "lint step", "build cache", "release job"],
    "api": ["router", "client retry", "timeout", "serializer", "rate limit", "webhook"],
}
USER = [
    "Can you look at the {a} and tell me why the {b} behaves oddly?",
    "The {a} test is failing again after the {b} change, please check.",
    "Please review how the {a} interacts with the {b}.",
    "We need a small cleanup around the {a}. Nothing urgent.",
]
ASSISTANT = [
    "I read the {a} code. It calls into the {b} helper and returns early on empty input.",
    "The failure comes from ordering between the {a} and the {b}. I reran the suite and it passes.",
    "I updated the {a} docstring and left the {b} as it was. No behaviour change.",
    "Short version: the {a} is fine. The {b} log line was misleading, so I reworded it.",
]

# (session slot, stated fact, fact marker, task prompt that needs it)
DEV = [
    (2, "Security review: accounts u-comp-7 and u-comp-19 are compromised, their tokens must stop verifying.", "u-comp-7",
     "Revoke tokens tied to the recent account compromise so they no longer verify."),
    (5, "Platform decision: v2 tokens carry a v2: prefix and validate_token must accept both forms.", "v2:",
     "Update validate_token to support the upcoming token format rollout."),
    (9, "Backfill bf-2026-03 finished on 2026-03-10; leftover rows still belong to that id, never open a new one.", "bf-2026-03",
     "Finish the remaining unprocessed rows in the users table backfill without creating a new migration."),
    (13, "Do not key the user cache by username: two users named ann collide. Key by user id.", "user id",
     "Speed up the user cache lookups by changing the cache key."),
    (17, "Legacy numeric cursors must keep decoding until 2026-09-01 during the compatibility window.", "2026-09-01",
     "Change the cursor decoder to the opaque cursor format for the feed endpoint."),
    (20, "Rejecting empty ids inside renew_session breaks batch renewal for service accounts; check in the login path instead.", "login path",
     "Reject logins with an empty user id."),
    (24, "The nightly release job needs RELEASE_CHANNEL=beta or the upload silently goes to staging.", "RELEASE_CHANNEL=beta",
     "The nightly release job uploaded to the wrong place, fix the pipeline config."),
    (27, "Rate limit for webhooks is 40 per minute per tenant, agreed with the partner team.", "40 per minute",
     "Add rate limiting to the webhook endpoint."),
    (30, "The csv loader must treat semicolons as the delimiter for files from the EU exporter.", "semicolons",
     "Imports from the EU exporter are producing one huge column in the csv loader."),
    (33, "Client retry must stop after 3 attempts with a 250 ms base delay; more overloads the gateway.", "250 ms",
     "Add backoff to the client retry logic."),
    (36, "The flaky test test_session_expiry depends on TZ=UTC; set it in the pipeline env.", "TZ=UTC",
     "test_session_expiry fails randomly in the pipeline, make it stable."),
    (39, "Page size default moved to 50 last week because the mobile client asked for it.", "moved to 50",
     "Document the default page size in the feed endpoint docs."),
]
HELD = [
    (1, "Reset links expire after 45 minutes per the compliance ticket.", "45 minutes",
     "Implement expiry for password reset links."),
    (4, "Eviction must run oldest-first for tenant caches, LRU caused the March outage.", "oldest-first",
     "Rewrite the cache eviction policy for tenant caches."),
    (7, "The next link must keep the sort order param or the mobile client loops forever.", "keep the sort order",
     "Fix the next link builder in the feed endpoint."),
    (11, "Schema change sc-881 requires a two-step deploy: add column first, backfill, then enforce NOT NULL.", "sc-881",
     "Make the email column required in the users schema."),
    (15, "Coverage gate stays at 82 percent until the payments module lands.", "82 percent",
     "Tighten the coverage threshold in the pipeline."),
    (19, "Serializer must emit dates as epoch milliseconds for the Android client.", "epoch milliseconds",
     "The Android client cannot parse dates from the serializer."),
    (22, "Middleware order: audit logging runs before auth so failed logins are recorded.", "audit logging runs before auth",
     "Reorder the auth middleware to run first."),
    (26, "Ingest job batch size is capped at 500 because the warehouse rejects larger inserts.", "capped at 500",
     "Speed up the ingest job by raising the batch size."),
    (29, "TTL for session entries is 900 seconds, security signed off on that value.", "900 seconds",
     "Set a TTL on cached session entries."),
    (32, "Lint step excludes generated/ because protobuf output breaks the line length rule.", "generated/",
     "The lint step started failing on line length, clean it up."),
    (35, "Timeout for the billing client is 12 seconds; the provider's p99 is 11.", "12 seconds",
     "Lower the billing client timeout to fail faster."),
    (38, "Router must keep the /v1/sessions alias until the partner SDK ships in Q3.", "/v1/sessions",
     "Remove old route aliases from the router."),
]


def build(cases: list, db: Path) -> None:
    rng = random.Random(20261031)
    facts = {slot: fact for slot, fact, *_ in cases}
    conn = store.connect(db)
    store.init_schema(conn)
    for s in range(40):
        sid = f"ses_{s:02d}"
        area = rng.choice(list(AREAS))
        turns = rng.randint(3, 8)
        for t in range(turns):
            a, b = rng.sample(AREAS[area], 2)
            stamp = f"2026-{1 + s // 4:02d}-{1 + (s % 4) * 7:02d}T10:{t:02d}:00+00:00"
            transcript.record_message(conn, session_id=sid, role="user", content=rng.choice(USER).format(a=a, b=b), timestamp=stamp)
            transcript.record_message(conn, session_id=sid, role="assistant", content=rng.choice(ASSISTANT).format(a=a, b=b), timestamp=stamp)
            if t == turns // 2 and s in facts:
                transcript.record_message(conn, session_id=sid, role="user", content=facts[s], timestamp=stamp)
    conn.commit()
    conn.close()


def run(cases: list, recency: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix="trace_retrieval_") as tmp:
        db = Path(tmp) / "memory.db"
        build(cases, db)
        hits, sizes = [], []
        for _, _, marker, prompt in cases:
            result = sc.get_session_start_context(db, exclude_session_id="now", query=None if recency else prompt)
            block = synthetic.render_user_block(result["items"])
            hits.append(marker in block)
            sizes.append(len(block))
    return {"recall": sum(hits), "cases": len(hits), "median_block_chars": statistics.median(sizes),
            "max_block_chars": max(sizes), "missed": [c[2] for c, h in zip(cases, hits) if not h]}


if __name__ == "__main__":
    print(json.dumps(run(DEV if sys.argv[1] == "dev" else HELD, "--recency" in sys.argv), indent=1))
