"""Preregistered Phase 3 analysis: pass rates, costs, task-cluster bootstrap CIs."""

import json
import random
import statistics
import sys
from collections import defaultdict

rows = [json.loads(x) for f in sys.argv[1:] for x in open(f, encoding="utf-8") if x.strip()]
hist = [r for r in rows if r.get("phase") == "history"]
B = [r for r in rows if r.get("phase") == "B"]
ARMS = ["N0", "N1", "T1", "T2", "N1U", "T2U"]
void = lambda r: r["rc"] == "timeout" and r["tools"] == 0


def tasks(subset):
    t = defaultdict(lambda: defaultdict(list))
    for r in subset:
        t[r["task"]][r["arm"]].append(r)
    return t


def boot(t, a, b, n=10000, seed=1):
    names = [k for k, v in t.items() if v.get(a) and v.get(b)]
    if not names:
        return None
    rate = lambda ts, arm: statistics.mean(r["pass"] for x in ts for r in t[x][arm])
    rng = random.Random(seed)
    sims = sorted(rate(s, a) - rate(s, b) for s in ([rng.choice(names) for _ in names] for _ in range(n)))
    return round(rate(names, a) - rate(names, b), 3), round(sims[int(0.025 * n)], 3), round(sims[int(0.975 * n)], 3)


def table(label, subset):
    t = tasks(subset)
    out = {"label": label, "n_rows": len(subset)}
    for arm in ARMS:
        x = [r for r in subset if r["arm"] == arm]
        if x:
            out[arm] = {"pass": f"{sum(r['pass'] for r in x)}/{len(x)}",
                        "median_tokens": statistics.median(r["tokens"]["input"] + r["tokens"]["cache_read"] for r in x),
                        "median_seconds": statistics.median(r["seconds"] for r in x),
                        "median_tools": statistics.median(r["tools"] for r in x),
                        "memory_tool_runs": sum(r["memory_tools"] > 0 for r in x),
                        "median_block": statistics.median(r["injected_chars"] for r in x),
                        "escaped": sum(r["escaped"] for r in x)}
    out["ci"] = {f"{a}-{b}": boot(t, a, b) for a, b in
                 [("N1", "N0"), ("T2", "N0"), ("T2", "T1"), ("T2", "N1"), ("T2U", "N1U")]}
    out["per_task"] = {k: {a: f"{sum(r['pass'] for r in v[a])}/{len(v[a])}" for a in ARMS if v.get(a)} for k, v in sorted(t.items())}
    return out


print(json.dumps({
    "history": [{k: h.get(k) for k in ("task", "handoff", "rcs", "notes_chars", "notes_has_fact", "store_has_fact")} for h in hist],
    "errors": [r for r in rows if r.get("phase") == "task_error"],
    "all_rows": table("all rows (preregistered)", B),
    "no_void": table("void rows removed", [r for r in B if not void(r)]),
    "clean": table("void and escaped rows removed", [r for r in B if not void(r) and not r["escaped"]]),
}, indent=1))
