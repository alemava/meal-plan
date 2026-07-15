# mesa — DeepInfra model benchmark

Compares 3 DeepInfra models for mesa's recipe-generation contract:
`meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo`, `mistralai/Mistral-Small-24B-Instruct-2501`,
`google/gemma-3-27b-it`.

Benchmarks against mesa's **real** production contract — `mesa_contract.py` imports
directly from `app/services/tools.py`, `guardrails.py`, `generation_rules.py`,
`recipe_audit.py`. Not a generic "write a recipe" prompt or an invented schema.

## Run

```bash
cd backend
export DEEPINFRA_API_KEY=$(grep '^DEEPINFRA_API_KEY=' .env | cut -d= -f2)
venv/bin/python -m benchmarks.runner
```

Takes a few minutes, costs well under $1 total (all 3 models are cheap per-token).
Writes:

- `results/raw/*.jsonl` — every single call's full raw response (gitignored — this is
  audit data for the person running it, not something to commit)
- `results/summary.csv` — every metric, machine-readable
- `results/REPORT.md` — the human-readable report with the final recommendation

## Rounds

- **R0** — tool-calling smoke test (1 call/model), confirms each model actually
  supports function-calling on DeepInfra before spending the rest of the budget.
- **R1** — core matrix: 24 realistic slot-request cases (`fixtures.py`) × 3 models,
  up to 2 fresh attempts each (mirrors `fresh_generation.MAX_GENERATION_ATTEMPTS`'s
  "brand new conversation per retry" pattern). This is the primary, decision-driving
  round — cost, latency, validity, semantic quality all come from here.
- **R4** — concurrency scaling: a 6-case subset run at concurrency 1/3/5/10, since a
  real mesa batch (see `generate_recipes._run_generation`) fires several concurrent
  calls, not one at a time.
- **R3-lite** — 3 recipes requested in a single tool call, testing whether batching
  same-meal-type slots into one call (the architecture question raised earlier this
  session) is viable: does the model respect the count, keep them valid, and keep
  them genuinely distinct.
- **RS** — steps-generation contract (`steps_generation.py`'s real prompt/schema/
  validator), using R1's own successfully-generated recipes as real input.

Deliberately not run this pass (see REPORT.md's own "deferred" section for why):
JSON-mode adapter path (R2), and the full R3 sweep across all count×concurrency
combinations.
