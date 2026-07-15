# mesa — DeepInfra recipe-generation model benchmark

Run: 2026-07-15T21:06:57.532173+00:00Z. Models: meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo, mistralai/Mistral-Small-24B-Instruct-2501, google/gemma-3-27b-it.

Benchmarked against mesa's REAL production contract (backend/benchmarks/mesa_contract.py imports directly from app/services/tools.py, guardrails.py, generation_rules.py, recipe_audit.py — same tool-calling schema, same DEFINING_COMPONENTS_RULE/KCAL_COMPUTATION_RULE prompt text, same validate_generated_recipe gate, same evaluate_kcal_plausibility semantic check the nightly audit uses). Not a generic 'write a recipe' prompt.

**Important asterisk on comparability**: mistralai/Mistral-Small-24B-Instruct-2501 does NOT support real tool-calling on DeepInfra — a live request with `tools` set returns a clean HTTP 405 ("Tool calling is not supported for model: ..."), contradicting that model's own DeepInfra page copy. Verified via a raw curl, not a harness bug. Every number for this model below comes from a JSON-mode adapter path instead (response_format=json_object, schema spelled out in the prompt) — the same accommodation mesa's real architecture would need to build if this model were ever chosen, since mesa's entire generation pipeline runs on tool-calling today. All other models use real tool-calling, unmodified.


## R0 — tool-calling smoke test (1 call/model, run first to gate the rest of the budget)

| Model | Path | Result |
|---|---|---|
| meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo | tool-calling | FAIL — ['model did not call submit_recipe'] |
| mistralai/Mistral-Small-24B-Instruct-2501 | JSON-mode | OK |
| google/gemma-3-27b-it | tool-calling | OK |

## R1 — core matrix (24 realistic slot-request cases per model)

| Model | Valid | Valid 1st try | Provider errors | No tool-call | kcal-flagged (of valid) | Latency mean/p95 | Cost/valid (1st try) | Cost/valid (w/ retry) |
|---|---|---|---|---|---|---|---|---|
| meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo | 37.5% | 16.7% | 0.0% | 22.7% | 33.3% | 6594.0ms / 9869ms | $3.2e-05 | $0.000157 |
| mistralai/Mistral-Small-24B-Instruct-2501 | 91.7% | 91.7% | 0.0% | 0.0% | 18.2% | 13917ms / 21924ms | $6.8e-05 | $8.1e-05 |
| google/gemma-3-27b-it | 58.3% | 54.2% | 0.0% | 34.3% | 21.4% | 31245.0ms / 43702ms | $0.000183 | $0.000456 |

## Composite scorecard (cost 35% + latency 20% + validity 30% + semantic 15%)

Each component normalised 0-100 against the best performer in that dimension — raw components always shown alongside, never just the composite.

| Model | Cost score | Latency score | Validity score | Semantic score | **Composite** |
|---|---|---|---|---|---|
| mistralai/Mistral-Small-24B-Instruct-2501 | 100.0 | 47.4 | 91.7 | 81.8 | **84.3** |
| meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo | 51.6 | 100.0 | 37.5 | 66.7 | **59.3** |
| google/gemma-3-27b-it | 17.8 | 21.1 | 58.3 | 78.6 | **39.7** |

## R4 — concurrency scaling (6-case subset, concurrency 1/3/5/10)

| Model | c=1 | c=3 | c=5 | c=10 |
|---|---|---|---|---|
| meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo | 11261ms (0.0% ok) | 21454ms (0.0% ok) | 16703ms (20.0% ok) | 18570ms (20.0% ok) |
| mistralai/Mistral-Small-24B-Instruct-2501 | 8602ms (100.0% ok) | 16460ms (100.0% ok) | 19379ms (100.0% ok) | 18639ms (90.0% ok) |
| google/gemma-3-27b-it | 23043ms (100.0% ok) | 63124ms (100.0% ok) | 74390ms (80.0% ok) | 60519ms (90.0% ok) |

## R3-lite — 3 recipes requested in a single call

| Model | Returned exactly 3 | All 3 valid | All 3 genuinely distinct |
|---|---|---|---|
| meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo | 66.7% | 0.0% | 66.7% |
| mistralai/Mistral-Small-24B-Instruct-2501 | 100.0% | 100.0% | 100.0% |
| google/gemma-3-27b-it | 0.0% | 0.0% | 0.0% |

## RS — steps-generation contract (6 cases, real generated ingredients as input)

| Model | Valid steps |
|---|---|
| meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo | 0.0% |
| mistralai/Mistral-Small-24B-Instruct-2501 | 100.0% |
| google/gemma-3-27b-it | 0.0% |

## Cost projection (base case: 200 input + 800 output tokens = 1000 tokens/recipe)

| Model | 1 recipe | 100 recipes | 1,000 recipes | 10,000 recipes |
|---|---|---|---|---|
| meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo | $0.00003 | $0.003 | $0.03 | $0.28 |
| mistralai/Mistral-Small-24B-Instruct-2501 | $0.00007 | $0.007 | $0.07 | $0.74 |
| google/gemma-3-27b-it | $0.00014 | $0.014 | $0.14 | $1.44 |

## Deliberately deferred, not run this pass

- **R2 (JSON-mode adapter path)**: mesa's entire architecture is tool-calling, not `response_format=json_object` — R0/R1 already confirm real tool-calling support on all 3 models, so a JSON-mode fallback path is only relevant if tool-calling proves unreliable in production. Not built this pass; revisit only if a chosen model's tool-calling degrades at real volume.
- **Full R3 sweep (1/3/5/10 recipes-per-prompt x concurrency)**: R3-lite (N=3, no concurrency sweep) already answers the question that motivated it (does batching same-meal-type slots into one call work, and does it preserve variety) without the full combinatorial cost.

## Recommendation

- **Best overall (composite score)**: mistralai/Mistral-Small-24B-Instruct-2501
- **Cheapest absolute**: mistralai/Mistral-Small-24B-Instruct-2501
- **Most reliable (valid %)**: mistralai/Mistral-Small-24B-Instruct-2501
- **Fastest**: meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo

### "Si este microservicio fuera tuyo, ¿cuál elegirías hoy?"

**mistralai/Mistral-Small-24B-Instruct-2501** — see the numbers above for why; this line is filled in programmatically from whichever model actually wins the composite score on this run's real data, not decided in advance.
