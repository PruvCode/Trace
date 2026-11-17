#!/usr/bin/env bash
# Queued behind run_all.sh: waits for the full suite, then runs the prose follow-up,
# regrades everything, and runs the per-clause diagnosis. Never overlaps Ollama use.
set -euo pipefail
cd $HOME/trace-framing-probe
S="$HOME/AppData/Local/Temp/claude/7ddaedbc3ef3ec90/88b3784d-0d46-4b7d-8adc-055a92c4e482/scratchpad/framing-scratch"
W="$HOME/Documents/Codex/2026-11-14/referenced-chatgpt-conversation-this-is-an/work"
WASI="$HOME/Documents/Codex/2026-11-11/referenced-chatgpt-conversation-this-is-an/work/p5-cpython"
PY="$W/trace-ci/Scripts/python.exe"
R=research/preview/framing-results
COMMON=(--trace $HOME/TRACE-graph-source --click "$W/eval-click" --packaging "$W/eval-packaging"
        --scratch "$S" --wasi "$WASI" --contexts $R/contexts.json)
SMOKE=(--task trace:memory/store.py:clamp_limit --task click:src/click/formatting.py:iter_rows)

until grep -q 'BATCH full done' $R/run_all.log; do sleep 60; done

"$PY" -m research.preview.framing_probe run "${COMMON[@]}" "${SMOKE[@]}" --output $R/smoke-greedy.jsonl \
  --run greedy --seed 52183 --variant trace_prose
echo "BATCH prose-greedy done"
SEEDS=(); for s in 52183 52189 52193 52199 52201; do SEEDS+=(--seed $s); done
"$PY" -m research.preview.framing_probe run "${COMMON[@]}" "${SMOKE[@]}" --output $R/smoke-sampled.jsonl \
  --run sampled --temperature 0.8 "${SEEDS[@]}" --variant trace_prose
echo "BATCH prose-sampled done"

for f in full-greedy smoke-greedy smoke-sampled; do
  "$PY" -m research.preview.framing_probe regrade "${COMMON[@]}" --output $R/$f.jsonl --regrade-output $R/regrade-$f.jsonl
done
echo "BATCH regrade done"
"$PY" -m research.preview.framing_clauses --wasi "$WASI" --input $R/full-greedy.jsonl --output $R/clauses-full.jsonl
"$PY" -m research.preview.framing_clauses --wasi "$WASI" --input $R/smoke-greedy.jsonl --input $R/smoke-sampled.jsonl --output $R/clauses-smoke.jsonl
echo "BATCH clauses done"
