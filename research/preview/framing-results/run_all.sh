#!/usr/bin/env bash
# Sequential framing-probe batches (one Ollama model at a time; never parallel).
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

# 1. Greedy: the added 2x2 cell on the smoke tasks (seeds are no-ops at temperature 0).
"$PY" -m research.preview.framing_probe run "${COMMON[@]}" "${SMOKE[@]}" --output $R/smoke-greedy.jsonl \
  --run greedy --seed 52183 --variant trace_revised_rule_only
echo "BATCH greedy-cell done"

# 2. Sampled replication on the smoke tasks: Ollama's default temperature (0.8), 10 recorded seeds.
SEEDS=(); for s in 52183 52189 52193 52199 52201 52207 52223 52237 52249 52253; do SEEDS+=(--seed $s); done
"$PY" -m research.preview.framing_probe run "${COMMON[@]}" "${SMOKE[@]}" --output $R/smoke-sampled.jsonl \
  --run sampled --temperature 0.8 "${SEEDS[@]}"
echo "BATCH sampled done"

# 3. Full frozen 24-task suite, greedy, all six variants.
"$PY" -m research.preview.framing_probe run "${COMMON[@]}" --output $R/full-greedy.jsonl --run greedy --seed 52183
echo "BATCH full done"
