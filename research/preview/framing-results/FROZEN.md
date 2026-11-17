# Framing probe: frozen 2026-11-16

Development ablation on the 24 previously seen maintained-notes tasks. Not held-out evidence.

- Code under test: `74f3096` (eval/trace-maintained-notes). Production renderer, `notes_trial.py`, `notes_cases.py`, `editor.py`, grader: unchanged.
- Runner: `research/preview/framing_probe.py`; clause diagnosis: `research/preview/framing_clauses.py`.
- Contexts: `contexts.json` (sha256 288405acb87149665c9821f53ba083e6d6cb712d08a404241d9b9676c137fb92); `trace_revised_rule_only` and `trace_prose` are derived from its `trace` block at run time.
- Rows: `full-greedy.jsonl`, `smoke-greedy.jsonl`, `smoke-sampled.jsonl` (raw, including duplicate rows from an accidental second process).
- Analysis key: (task, variant, seed, run, temperature, prompt_sha256); see `summary-*.txt`.
- Regrades: `regrade-*.jsonl` (all reproduced). Clause diagnosis: `clauses-*.jsonl`.
- Report: `REPORT.md`.

Do not append rows to these files. New experiments write new files.
