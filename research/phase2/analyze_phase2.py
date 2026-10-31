"""Preregistered Phase 2 analysis: pass rates and task-cluster bootstrap CIs."""

import json
import random
import statistics
import sys
from collections import defaultdict

rows = [json.loads(x) for f in sys.argv[1:] for x in open(f, encoding="utf-8") if x.strip()]
A = {r["task"]: r for r in rows if r.get("phase") == "A"}
B = [r for r in rows if r.get("phase") == "B"]
ARMS = ["C0", "C1", "C2", "C3"]


def per_task(subset):
    t = defaultdict(lambda: defaultdict(list))
    for r in subset:
        t[r["task"]][r["cond"]].append(r)
    return t


def rate(tasks, arm, key=lambda r: r["pass"]):
    vals = [key(r) for runs in tasks.values() for r in runs.get(arm, [])]
    return sum(vals) / len(vals) if vals else float("nan"), len(vals)


def boot(tasks, a, b, n=10000, seed=1):
    names = [k for k, v in tasks.items() if v.get(a) and v.get(b)]
    if not names:
        return None
    diff = lambda ts: (statistics.mean(r["pass"] for t in ts for r in tasks[t][a])
                       - statistics.mean(r["pass"] for t in ts for r in tasks[t][b]))
    rng = random.Random(seed)
    sims = sorted(diff([rng.choice(names) for _ in names]) for _ in range(n))
    return round(diff(names), 3), round(sims[int(0.025 * n)], 3), round(sims[int(0.975 * n)], 3)


out = {"A_sessions": {k: {x: v.get(x) for x in ("steps", "tools", "agents_md_has_facts", "store_has_facts")}
                      for k, v in A.items()}}
for fam in ("C_episodic", "D_staleness", "all"):
    sub = per_task([r for r in B if fam == "all" or r["task"].startswith(fam)])
    if not sub:
        continue
    res = {"tasks": len(sub)}
    for arm in ARMS:
        p, n = rate(sub, arm)
        res[arm] = {"pass_rate": round(p, 3), "n": n,
                    "median_tools": statistics.median(r["tools"] for runs in sub.values() for r in runs.get(arm, []))
                    if n else None,
                    "median_seconds": statistics.median(r["seconds"] for runs in sub.values() for r in runs.get(arm, []))
                    if n else None,
                    "median_input_tokens": statistics.median(r["tokens"]["input"] + r["tokens"]["cache_read"]
                                                             for runs in sub.values() for r in runs.get(arm, []))
                    if n else None}
    res["diff_ci95"] = {f"{a}-{b}": boot(sub, a, b) for a, b in
                        [("C1", "C0"), ("C2", "C0"), ("C3", "C0"), ("C3", "C2"), ("C2", "C1"), ("C3", "C1")]}
    res["per_task_pass"] = {t.split("/")[1]: {arm: f"{sum(r['pass'] for r in v.get(arm, []))}/{len(v.get(arm, []))}"
                                              for arm in ARMS} for t, v in sorted(sub.items())}
    out[fam] = res
print(json.dumps(out, indent=1))
