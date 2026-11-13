"""Run the frozen Phase 4 comparison against temporary synthetic TRACE databases."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import random
import re
import sqlite3
import statistics
import tempfile
from contextlib import closing
from pathlib import Path
from time import perf_counter

from memory import episodic, redact, session_context, store, synthetic, task_library

CASES = Path(__file__).with_name("cases.json")
MODEL_FILES = {
    "model_quint8_avx2.onnx": "b941bf19f1f1283680f449fa6a7336bb5600bdcd5f84d10ddc5cd72218a0fd21",
    "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
}
MAX_ITEMS = 5
MAX_CHARS = 2000
REPEATS = 5
SEED = 4061
IDENTIFIER = re.compile(
    r"\b(?:[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*|"
    r"[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)+)\b"
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MiniLM:
    def __init__(self, model_dir):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        for name, expected in MODEL_FILES.items():
            if sha256(model_dir / name) != expected:
                raise ValueError(f"Model file hash mismatch: {name}")
        self.np = np
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=128)
        self.tokenizer.enable_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_dir / "model_quint8_avx2.onnx"), options,
            providers=["CPUExecutionProvider"],
        )

    def encode(self, texts):
        tokens = self.tokenizer.encode_batch(texts)
        inputs = {
            "input_ids": self.np.array([token.ids for token in tokens], dtype="int64"),
            "attention_mask": self.np.array([token.attention_mask for token in tokens], dtype="int64"),
            "token_type_ids": self.np.array([token.type_ids for token in tokens], dtype="int64"),
        }
        hidden = self.session.run(["last_hidden_state"], inputs)[0]
        mask = inputs["attention_mask"][..., None]
        # After normalization, the masked sum and masked mean have the same direction.
        pooled = (hidden * mask).sum(axis=1).astype("float32")
        norms = self.np.linalg.norm(pooled, axis=1, keepdims=True)
        if not self.np.isfinite(norms).all() or (norms <= 0).any():
            raise ValueError("Invalid embedding")
        return pooled / norms


def seed_database(path, groups, noise):
    cases = []
    with closing(store.connect(path)) as db:
        task_ids = {name: task_library.create_task(db, name, name)["id"] for name in groups}

        def record(name, message):
            return episodic.record_event(
                db, repo="synthetic/phase4", type="observation",
                task_id=task_ids[name], payload={"message": message},
            )

        for name, rows in groups.items():
            for number, (kind, query, note) in enumerate(rows, 1):
                cases.append({
                    "id": f"{name}-{number:02}", "task": name, "task_id": task_ids[name],
                    "kind": kind, "query": query, "gold": record(name, note),
                })
        for i, name in enumerate(groups):
            for stage in ("initial review", "canary rollout", "load exercise", "follow-up"):
                for note in noise:
                    record(name, f"{name} {stage}: {note}")
            # Every query has a newer, query-shaped decoy in a different task.
            other = list(groups)[i - 1]
            for _, query, _ in groups[other]:
                record(name, f"Open question in {name} shadow deployment: {query}")
    return cases


def prepare_index(path, cases, embed):
    task_ids = dict.fromkeys(case["task_id"] for case in cases)
    index = {}
    with closing(store.connect(path)) as db:
        for task_id in task_ids:
            events = episodic.search_events(db, task_id=task_id, newest_first=True, limit=store.MAX_LIMIT)
            # shortcut: this fixture contains only ordinary observation rows. Production
            # eligibility and freshness would require a separate integration test.
            assert all(event.type == "observation" and not event.evidence
                       and event.superseded_by is None for event in events)
            items = [{
                "kind": "finding", "role": event.type, "event_id": event.id,
                "task_id": event.task_id, "freshness": "unknown", "evidence": None,
                "text": session_context._truncate(
                    redact.redact_text(session_context._finding_text(event.payload)),
                    session_context.MAX_ITEM_CHARS,
                )[0],
            } for event in events]
            vectors = embed([item["text"] for item in items])
            assert vectors.shape == (len(items), 384)
            index[task_id] = items, vectors
    return index


def lexical(path, case):
    return session_context.get_session_start_context(
        path, task_id=case["task_id"], query=case["query"], max_items=MAX_ITEMS, max_chars=MAX_CHARS,
    )["items"]


def hybrid(path, case, index, embed):
    baseline = lexical(path, case)
    if IDENTIFIER.search(case["query"]):
        return baseline, "identifier"
    if embed is None or case["task_id"] not in index:
        return baseline, "unavailable"
    documents, vectors = index[case["task_id"]]
    try:
        (vector,) = embed([case["query"]])
        similarities = vectors @ vector
        if not all(map(math.isfinite, similarities)):
            raise ValueError("Invalid similarity")
    except Exception:
        return baseline, "inference_error"
    order = sorted(range(len(documents)), key=lambda i: (-similarities[i], documents[i]["event_id"]))
    combined = {item["event_id"]: (0.35 / (10 + rank), item)
                for rank, item in enumerate(baseline, 1)}
    for rank, i in enumerate(order[:20], 1):
        item = documents[i]
        event_id = item["event_id"]
        score, _ = combined.get(event_id, (0, item))
        combined[event_id] = (score + 0.65 / (10 + rank), item)
    items = [combined[i][1] for i in sorted(combined, key=lambda i: (-combined[i][0], i))[:MAX_ITEMS]]
    while items and len(synthetic.render_user_block(items)) > MAX_CHARS:
        items.pop()
    return items, "semantic"


def snapshot(items, case):
    ids = [item["event_id"] for item in items]
    return {
        "event_ids": ids,
        "rank": ids.index(case["gold"]) + 1 if case["gold"] in ids else None,
        "wrong_task": sum(item["task_id"] != case["task_id"] for item in items),
        "chars": len(synthetic.render_user_block(items)),
    }


def timed(function, *args):
    started = perf_counter()
    value = function(*args)
    return value, (perf_counter() - started) * 1000


def percentile(values, p):
    return sorted(values)[math.ceil(len(values) * p) - 1]


def summarize(rows, arm):
    ranks = [row[arm]["rank"] for row in rows]
    times = [ms for row in rows for ms in row[arm]["warm_ms"]]
    return {
        "n": len(rows),
        "hits": sum(rank is not None for rank in ranks),
        "recall_at_5": statistics.mean(rank is not None for rank in ranks),
        "mrr_at_5": statistics.mean(1 / rank if rank else 0 for rank in ranks),
        "wrong_task_items": sum(row[arm]["wrong_task"] for row in rows),
        "p50_ms": statistics.median(times),
        "p95_ms": percentile(times, 0.95),
        "median_chars": statistics.median(row[arm]["chars"] for row in rows),
        "max_chars": max(row[arm]["chars"] for row in rows),
    }


def run(scratch, model_dir):
    fixture = json.loads(CASES.read_text(encoding="utf-8"))
    dev_path = scratch / "dev.sqlite"
    db_path = scratch / "heldout.sqlite"
    dev = seed_database(dev_path, fixture["development"], fixture["noise"])
    heldout = seed_database(db_path, fixture["heldout"], fixture["noise"])
    encoder, initial_load = timed(MiniLM, model_dir)
    dev_index = prepare_index(dev_path, dev, encoder.encode)
    for case in dev:
        items, status = hybrid(dev_path, case, dev_index, encoder.encode)
        assert items and status == ("semantic" if case["kind"] == "paraphrase" else "identifier")
        assert snapshot(items, case)["wrong_task"] == 0
    first = dev[0]
    expected = lexical(dev_path, first)

    def failing_encoder(_texts):
        raise RuntimeError("synthetic inference failure")

    assert hybrid(dev_path, first, dev_index, None) == (expected, "unavailable")
    assert hybrid(dev_path, first, dev_index, failing_encoder) == (expected, "inference_error")

    index, build_ms = timed(prepare_index, db_path, heldout, encoder.encode)
    assert all(case["gold"] in {item["event_id"] for item in index[case["task_id"]][0]}
               for case in heldout)
    assert all(len(items) <= store.MAX_LIMIT for items, _ in index.values())

    rows = {case["id"]: {key: case[key] for key in ("id", "kind", "task", "gold")} for case in heldout}
    rng = random.Random(SEED)
    for _ in range(REPEATS):
        schedule = heldout.copy()
        rng.shuffle(schedule)
        for case in schedule:
            arms = ["fts", "hybrid"]
            rng.shuffle(arms)
            for arm in arms:
                if arm == "fts":
                    items, ms = timed(lexical, db_path, case)
                    status = "fts"
                else:
                    (items, status), ms = timed(hybrid, db_path, case, index, encoder.encode)
                assert status in ("fts", "identifier", "semantic")
                result = snapshot(items, case)
                assert items and len(items) <= MAX_ITEMS and result["chars"] <= MAX_CHARS
                result["route"] = status
                row = rows[case["id"]]
                if arm not in row:
                    row[arm] = {**result, "warm_ms": []}
                assert all(row[arm][key] == value for key, value in result.items())
                row[arm]["warm_ms"].append(ms)

    paraphrases = [row for row in rows.values() if row["kind"] == "paraphrase"]
    exact = [row for row in rows.values() if row["kind"] == "exact"]
    metrics = {group: {arm: summarize(part, arm) for arm in ("fts", "hybrid")}
               for group, part in (("all", list(rows.values())), ("paraphrase", paraphrases), ("exact", exact))}
    fts = metrics["paraphrase"]["fts"]
    candidate = metrics["paraphrase"]["hybrid"]
    delta = candidate["recall_at_5"] - fts["recall_at_5"]
    exact_losses = [row["id"] for row in exact if row["fts"]["rank"] and not row["hybrid"]["rank"]]
    latency_ratio = candidate["p95_ms"] / fts["p95_ms"]
    gates = {
        "paraphrase_recall_gain": delta >= 0.10,
        "exact_no_lost_hits": not exact_losses,
        "zero_wrong_task_items": all(metrics["all"][arm]["wrong_task_items"] == 0 for arm in ("fts", "hybrid")),
        "paraphrase_p95_within_25_percent": latency_ratio <= 1.25,
    }
    task_deltas = [
        statistics.mean(int(row["hybrid"]["rank"] is not None) - int(row["fts"]["rank"] is not None)
                        for row in paraphrases if row["task"] == task)
        for task in fixture["heldout"]
    ]
    bootstrap = random.Random(SEED)
    bootstrap_means = [statistics.mean(bootstrap.choices(task_deltas, k=len(task_deltas)))
                       for _ in range(2000)]
    cold = []
    for task in fixture["heldout"]:
        case = next(case for case in heldout if case["task"] == task and case["kind"] == "paraphrase")
        _, fts_ms = timed(lexical, db_path, case)
        fresh, load_ms = timed(MiniLM, model_dir)
        (_, status), lookup_ms = timed(hybrid, db_path, case, index, fresh.encode)
        assert status == "semantic"
        cold.append({"id": case["id"], "fts_ms": fts_ms, "model_load_ms": load_ms,
                     "hybrid_lookup_ms": lookup_ms, "hybrid_total_ms": load_ms + lookup_ms})
        del fresh
    with closing(store.connect(db_path)) as db:
        event_rows = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return {
        "fixture_sha256": sha256(CASES), "harness_sha256": sha256(Path(__file__)),
        "platform": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "seed": SEED, "repeats": REPEATS, "case_results": list(rows.values()), "metrics": metrics,
        "gates": gates, "keep_embeddings": all(gates.values()), "exact_lost_hits": exact_losses,
        "paraphrase_gain": delta, "paraphrase_p95_ratio": latency_ratio,
        "paraphrase_task_bootstrap_95": [percentile(bootstrap_means, p) for p in (0.025, 0.975)],
        "initial_model_load_ms": initial_load, "build_ms": build_ms,
        "index_rows": sum(len(items) for items, _ in index.values()), "db_rows": event_rows,
        "vector_bytes": sum(vectors.nbytes for _, vectors in index.values()),
        "db_bytes": db_path.stat().st_size, "model_bytes": sum((model_dir / name).stat().st_size for name in MODEL_FILES),
        "fallback_checks": {"missing_index_or_model": True, "inference_failure": True},
        "cold_model_sessions_warm_os_cache": cold,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(dir=args.scratch, prefix="trace-phase4-") as scratch:
        print(json.dumps(run(Path(scratch), args.model_dir), indent=2))


if __name__ == "__main__":
    main()
