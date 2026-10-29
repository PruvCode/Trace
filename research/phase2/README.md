# research/phase2 — controlled C0–C3 evaluation (preregistered)

**Binding design:** `docs/autopilot/PHASE2_PREREGISTRATION.md` (written before any Phase-2
result by the prior agent; adopted verbatim). Any run used for analysis must follow it:
10 tasks (6× C_episodic, 4× D_staleness), conditions C0/C1/C2/C3, planned reps = 2 per
condition per task (a third rep only if decided before looking at outcome differences),
per-task shuffled condition order with fixed seed 20261028, OpenCode 1.18.32,
model `opencode/big-pickle`, hidden `eval_command` scoring, outco‌me rows as JSONL.

## Files

- `run_phase2.py` — harness. Adopted from the prior agent's branch (committed state at
  `4363ff4`, byte-identical) with ONE GLM change: `PY` no longer hardcodes a OneDrive
  venv path — it uses `sys.executable` so the harness runs from any checkout.
- `run1.jsonl`, `smoke.jsonl` — the prior agent's PARTIAL data (91 + 6 rows): A-phase
  rows and a few B-phase conditions for the two C_episodic tasks, produced on its own
  worktree with the equivalent memory code (baedd4b = the per-request policy adopted in
  PR #1). Treated as prior-agent partial data, NOT merged into our dataset.
- `analyze_phase2.py` — prior agent's analysis script (unaudited until used).

## Operating rules for GLM runs

1. Engineering smoke runs (harness validation) write to a separate root and are
   **excluded from the preregistered analysis dataset**; they are validation-only.
2. The preregistered run uses: `python research/phase2/run_phase2.py <root> 2 <workers>`
   over all 10 tasks, JSONL teed to a results file.
3. Fixtures must be seeded first (`scripts/seed_*.py`); base_commit pins verified before
   launch. `fixtures/history_repo` must be freshly seeded (untracked; seeder-owned).
4. Machine-sleep caveat: use a machine that stays awake; runs are resumable only by
   re-running whole tasks (no incremental resume in the harness).
