#!/usr/bin/env bash
set -euo pipefail
cd /c/Users/pruth/trace-rules
S="C:/Users/pruth/AppData/Local/Temp/claude/7ddaedbc3ef3ec90/88b3784d-0d46-4b7d-8adc-055a92c4e482/scratchpad/repair-scratch"
W='C:/Users/pruth/Documents/Codex/2026-11-14/referenced-chatgpt-conversation-this-is-an/work'
WASI='C:/Users/pruth/Documents/Codex/2026-11-11/referenced-chatgpt-conversation-this-is-an/work/p5-cpython'
PY="$W/trace-ci/Scripts/python.exe"
R=research/preview/repair-results
C=(--trace C:/Users/pruth/TRACE-graph-source --click "$W/eval-click" --packaging "$W/eval-packaging" --scratch "$S" --wasi "$WASI")
"$PY" -m research.preview.repair_trial run "${C[@]}" --output $R/smoke-greedy.jsonl --run smoke-greedy \
  --task trace:memory/store.py:clamp_limit --task click:src/click/formatting.py:iter_rows
echo "BATCH smoke done"
"$PY" -m research.preview.repair_trial run "${C[@]}" --output $R/full-greedy.jsonl --run greedy
echo "BATCH greedy done"
"$PY" -m research.preview.repair_trial regrade "${C[@]}" --output $R/full-greedy.jsonl --regrade-output $R/regrade-full-greedy.jsonl
echo "BATCH greedy-regrade done"
"$PY" -m research.preview.repair_trial run "${C[@]}" --output $R/full-sampled.jsonl --run sampled --temperature 0.8 \
  --seed 52183 --seed 52189 --seed 52193
echo "BATCH sampled done"
"$PY" -m research.preview.repair_trial regrade "${C[@]}" --output $R/full-sampled.jsonl --regrade-output $R/regrade-full-sampled.jsonl
echo "BATCH sampled-regrade done"
