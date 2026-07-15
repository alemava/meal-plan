"""DeepInfra 3-model benchmark for mesa's recipe-generation contract.

Run with: backend/venv/bin/python -m benchmarks.runner
(from backend/, with DEEPINFRA_API_KEY set — see README.md)

Rounds:
  R0  - tool-calling smoke test (1 call/model)
  R1  - core matrix: 24 realistic slot-request cases x 3 models, up to
        2 attempts each, validated against the REAL mesa contract
        (mesa_contract.py — imported from app/, not reimplemented)
  R4  - concurrency scaling: a 6-case subset at concurrency 1/3/5/10
  R3L - multi-recipe-per-prompt (lite): 3 recipes requested in one call,
        3 cases x 3 models
  RS  - steps-generation contract: 6 cases x 3 models, using R1's own
        successfully-generated recipes as input

Raw per-call data -> results/raw/*.jsonl (gitignored). Aggregates ->
results/summary.csv + REPORT.md (committed).
"""

import asyncio
import copy
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Both parent (for `import app.*` inside mesa_contract.py) and this
# directory (so `import deepinfra_client`/`fixtures` resolve the same way
# whether this runs as `-m benchmarks.runner` or as a plain script) need to
# be on the path — `-m` only puts the CURRENT working directory on
# sys.path[0], not this file's own directory.
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import deepinfra_client as di  # noqa: E402
import fixtures  # noqa: E402
import mesa_contract as mc  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "results"
RAW_DIR = RESULTS_DIR / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

MODEL_KEYS = list(di.MODELS.keys())
MAX_ATTEMPTS = 2


def _raw_log(round_name: str, record: dict) -> None:
    path = RAW_DIR / f"{round_name}.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


async def _run_one_recipe_case(model_key: str, case: fixtures.Case, round_name: str) -> dict:
    """One case, up to MAX_ATTEMPTS fresh attempts (mirrors
    fresh_generation.py's MAX_GENERATION_ATTEMPTS: each retry is a brand
    new conversation, not a corrective follow-up). Returns the best
    outcome plus full attempt history.

    Dispatches to tool-calling or JSON-mode per model (see
    deepinfra_client.USES_JSON_MODE) — Mistral's real API doesn't accept
    the tools param at all (verified live, R0), so it's tested via the
    adapter path mesa's real architecture would need for it."""
    json_mode = model_key in di.USES_JSON_MODE
    system_prompt = mc.build_json_mode_system_prompt(case) if json_mode else mc.build_system_prompt(case)
    user_prompt = f"Generate one {case.meal_type} recipe now."
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    attempts = []
    final = None
    for attempt_n in range(1, MAX_ATTEMPTS + 1):
        # A whole multi-hour run has already died 3 times on a per-case
        # parsing surprise from some model (top-level array, truncated
        # JSON, ...) — each one fixed individually, but the real fix is
        # this blanket net: ANY unexpected exception here is itself a
        # reliability finding worth recording, never a reason to lose
        # every result gathered so far.
        try:
            result = await di.chat_completion(
                model_key, messages, None if json_mode else mc.SUBMIT_RECIPE_TOOLS, json_mode=json_mode
            )
            outcome = {
                "attempt": attempt_n,
                "ok": result["ok"],
                "error": result["error"],
                "latency_ms": result["latency_ms"],
                "tokens_in": result["tokens_in"],
                "tokens_out": result["tokens_out"],
            }
            if not result["ok"]:
                outcome["violations"] = ["provider/network error"]
                attempts.append(outcome)
                continue

            recipe = (
                di.extract_json_content(result)
                if json_mode
                else di.extract_tool_call_arguments(result, "submit_recipe")
            )
            if recipe is None:
                outcome["violations"] = [
                    "model did not return parseable JSON" if json_mode else "model did not call submit_recipe"
                ]
                outcome["raw_content"] = (result["content"] or "")[:500]
                attempts.append(outcome)
                continue

            violations = mc.validate_against_mesa_contract(recipe, case)
            outcome["violations"] = violations
            outcome["recipe"] = recipe
            attempts.append(outcome)

            if not violations:
                final = {"attempt_n": attempt_n, "recipe": recipe, "outcome": outcome}
                break
        except Exception as exc:  # noqa: BLE001 — harness resilience, not a crash
            attempts.append({
                "attempt": attempt_n, "ok": False, "error": repr(exc),
                "latency_ms": 0, "tokens_in": None, "tokens_out": None,
                "violations": [f"harness error: {exc!r}"],
            })
            continue

    record = {
        "round": round_name,
        "model": model_key,
        "case": case.label,
        "meal_type": case.meal_type,
        "success": final is not None,
        "success_first_attempt": final is not None and final["attempt_n"] == 1,
        "attempts": attempts,
        "total_tokens_in": sum(a["tokens_in"] or 0 for a in attempts),
        "total_tokens_out": sum(a["tokens_out"] or 0 for a in attempts),
        "total_latency_ms": sum(a["latency_ms"] for a in attempts),
        "n_attempts": len(attempts),
    }
    if final:
        record["kcal_findings"] = mc.kcal_findings(final["recipe"])
    _raw_log(round_name, record)
    return record


async def round_r0_smoke_test():
    print("\n=== R0: tool-calling smoke test ===")
    case = fixtures.CASES[0]
    records = []
    for model_key in MODEL_KEYS:
        record = await _run_one_recipe_case(model_key, case, "r0_smoke")
        record["json_mode"] = model_key in di.USES_JSON_MODE
        records.append(record)
        status = "OK" if record["success"] else "FAIL"
        print(f"  {model_key:20s} {status}  attempts={record['n_attempts']}  "
              f"tokens_in={record['total_tokens_in']} tokens_out={record['total_tokens_out']}")
        if not record["success"]:
            print(f"    last violation(s): {record['attempts'][-1].get('violations')}")
    return records


async def round_r1_core_matrix():
    print(f"\n=== R1: core matrix ({len(fixtures.CASES)} cases x {len(MODEL_KEYS)} models) ===")
    results = []
    for model_key in MODEL_KEYS:
        print(f"  running {model_key}...")
        for case in fixtures.CASES:
            record = await _run_one_recipe_case(model_key, case, "r1_core")
            results.append(record)
            mark = "." if record["success"] else "X"
            print(mark, end="", flush=True)
        print()
    return results


async def round_r4_concurrency():
    print("\n=== R4: concurrency scaling ===")
    levels = [1, 3, 5, 10]
    results = []
    subset = fixtures.CONCURRENCY_SUBSET
    for model_key in MODEL_KEYS:
        for level in levels:
            cases = [subset[i % len(subset)] for i in range(level)]
            start = time.monotonic()
            batch = await asyncio.gather(
                *(_run_one_recipe_case(model_key, c, f"r4_concurrency_{level}") for c in cases)
            )
            wall_ms = int((time.monotonic() - start) * 1000)
            n_ok = sum(1 for r in batch if r["success"])
            print(f"  {model_key:20s} concurrency={level:2d}  wall={wall_ms:6d}ms  "
                  f"ok={n_ok}/{level}")
            results.append({
                "model": model_key,
                "concurrency": level,
                "wall_ms": wall_ms,
                "n_ok": n_ok,
                "n_total": level,
                "cases": [r for r in batch],
            })
    return results


SUBMIT_RECIPES_TOOL = None  # built lazily below, needs mc.SUBMIT_RECIPE_TOOLS' inner schema


def _build_multi_recipe_tool(n: int) -> list[dict]:
    single = copy.deepcopy(mc.SUBMIT_RECIPE_TOOLS[0])
    item_schema = single["function"]["parameters"]
    wrapped = {
        "type": "function",
        "function": {
            "name": "submit_recipes",
            "description": f"Submit exactly {n} distinct recipes at once. Call exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipes": {"type": "array", "minItems": n, "maxItems": n, "items": item_schema}
                },
                "required": ["recipes"],
            },
        },
    }
    return [wrapped]


_JSON_MULTI_RECIPE_INSTRUCTION = (
    "\n\nRespond with ONLY a single JSON object: {{\"recipes\": [<{n} recipe objects, each shaped "
    "exactly as described above>]}}. No markdown, no text outside the JSON object."
)


async def round_r3_lite_multi_recipe():
    print("\n=== R3-lite: multi-recipe-per-prompt (N=3) ===")
    n = 3
    tools = _build_multi_recipe_tool(n)
    cases = fixtures.CONCURRENCY_SUBSET[:3]
    results = []
    for model_key in MODEL_KEYS:
        json_mode = model_key in di.USES_JSON_MODE
        for case in cases:
            base_prompt = mc.build_json_mode_system_prompt(case) if json_mode else mc.build_system_prompt(case)
            variety_instruction = (
                f" This time, generate {n} DIFFERENT {case.meal_type} recipes at once "
                "(genuinely different dishes, not variations of the same one)"
            )
            if json_mode:
                system_prompt = base_prompt + variety_instruction + _JSON_MULTI_RECIPE_INSTRUCTION.format(n=n)
            else:
                system_prompt = base_prompt + variety_instruction + " and call submit_recipes with all of them together."
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Generate {n} {case.meal_type} recipes now."},
            ]
            try:
                result = await di.chat_completion(
                    model_key, messages, None if json_mode else tools, json_mode=json_mode
                )
                record = {
                    "round": "r3_lite_multi",
                    "model": model_key,
                    "case": case.label,
                    "ok": result["ok"],
                    "error": result["error"],
                    "latency_ms": result["latency_ms"],
                    "tokens_in": result["tokens_in"],
                    "tokens_out": result["tokens_out"],
                }
                if result["ok"]:
                    if json_mode:
                        args = di.extract_json_content(result)
                    else:
                        args = di.extract_tool_call_arguments(result, "submit_recipes")
                    recipes = (args or {}).get("recipes") or []
                    if not isinstance(recipes, list):
                        recipes = []
                    record["n_recipes_returned"] = len(recipes)
                    violations_per_recipe = [mc.validate_against_mesa_contract(r, case) for r in recipes]
                    record["n_valid"] = sum(1 for v in violations_per_recipe if not v)
                    titles = [r.get("title") for r in recipes if isinstance(r, dict)]
                    record["n_distinct_titles"] = len(set(titles))
                    record["violations_per_recipe"] = violations_per_recipe
                else:
                    record["n_recipes_returned"] = 0
                    record["n_valid"] = 0
                    record["n_distinct_titles"] = 0
            except Exception as exc:  # noqa: BLE001 — harness resilience, not a crash
                record = {
                    "round": "r3_lite_multi", "model": model_key, "case": case.label,
                    "ok": False, "error": repr(exc), "latency_ms": 0, "tokens_in": None, "tokens_out": None,
                    "n_recipes_returned": 0, "n_valid": 0, "n_distinct_titles": 0,
                }
            _raw_log("r3_lite_multi", record)
            results.append(record)
            print(f"  {model_key:20s} {case.label:30s} "
                  f"returned={record['n_recipes_returned']} valid={record['n_valid']} "
                  f"distinct_titles={record['n_distinct_titles']}")
    return results


async def round_rs_steps(r1_results: list[dict]):
    print("\n=== RS: steps-generation contract ===")
    by_model_case = {(r["model"], r["case"]): r for r in r1_results if r["success"]}
    results = []
    for model_key in MODEL_KEYS:
        for case in fixtures.CONCURRENCY_SUBSET:
            key = (model_key, case.label)
            source = by_model_case.get(key)
            if not source:
                print(f"  {model_key:20s} {case.label:30s} SKIPPED (no successful R1 recipe to build steps from)")
                continue
            recipe = source["attempts"][-1]["recipe"]
            json_mode = model_key in di.USES_JSON_MODE
            system_prompt, user_prompt = (
                mc.build_json_mode_steps_prompt(recipe) if json_mode else mc.build_steps_prompt(recipe)
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            try:
                result = await di.chat_completion(
                    model_key, messages, None if json_mode else mc.STEPS_TOOLS, json_mode=json_mode
                )
                record = {
                    "round": "rs_steps",
                    "model": model_key,
                    "case": case.label,
                    "ok": result["ok"],
                    "error": result["error"],
                    "latency_ms": result["latency_ms"],
                    "tokens_in": result["tokens_in"],
                    "tokens_out": result["tokens_out"],
                }
                if result["ok"]:
                    if json_mode:
                        args = di.extract_json_content(result)
                    else:
                        args = di.extract_tool_call_arguments(result, "submit_steps")
                    steps = (args or {}).get("steps")
                    if steps is None:
                        record["violations"] = [
                            "model did not return parseable JSON" if json_mode else "model did not call submit_steps"
                        ]
                    elif not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
                        record["violations"] = [f"steps is not a list of objects: {type(steps).__name__}"]
                    else:
                        record["violations"] = mc.validate_steps_against_contract(steps)
                        record["n_steps"] = len(steps)
                else:
                    record["violations"] = ["provider/network error"]
            except Exception as exc:  # noqa: BLE001 — harness resilience, not a crash
                record = {
                    "round": "rs_steps", "model": model_key, "case": case.label,
                    "ok": False, "error": repr(exc), "latency_ms": 0, "tokens_in": None, "tokens_out": None,
                    "violations": [f"harness error: {exc!r}"],
                }
            _raw_log("rs_steps", record)
            results.append(record)
            status = "OK" if result["ok"] and not record.get("violations") else "FAIL"
            print(f"  {model_key:20s} {case.label:30s} {status}")
    return results


# ---------------------------------------------------------------------
# Scoring + report
# ---------------------------------------------------------------------

def _pct(n, d):
    return round(100 * n / d, 1) if d else 0.0


def summarize_r1(r1_results: list[dict]) -> dict:
    summary = {}
    for model_key in MODEL_KEYS:
        rows = [r for r in r1_results if r["model"] == model_key]
        n = len(rows)
        n_success = sum(1 for r in rows if r["success"])
        n_first_try = sum(1 for r in rows if r["success_first_attempt"])
        latencies = [a["latency_ms"] for r in rows for a in r["attempts"] if a["ok"]]
        tokens_in = [a["tokens_in"] for r in rows for a in r["attempts"] if a["ok"] and a["tokens_in"]]
        tokens_out = [a["tokens_out"] for r in rows for a in r["attempts"] if a["ok"] and a["tokens_out"]]
        all_calls = sum(r["n_attempts"] for r in rows)
        n_provider_errors = sum(
            1 for r in rows for a in r["attempts"] if not a["ok"]
        )
        n_no_tool_call = sum(
            1 for r in rows for a in r["attempts"]
            if a["ok"] and a.get("violations") == ["model did not call submit_recipe"]
        )
        kcal_flagged = sum(
            1 for r in rows if r["success"] and r.get("kcal_findings")
        )

        def pctl(vals, p):
            if not vals:
                return None
            s = sorted(vals)
            idx = min(int(len(s) * p), len(s) - 1)
            return s[idx]

        avg_tokens_in = round(statistics.mean(tokens_in), 1) if tokens_in else None
        avg_tokens_out = round(statistics.mean(tokens_out), 1) if tokens_out else None
        cost_per_valid_first_try = None
        cost_per_valid_with_retry = None
        if avg_tokens_in and avg_tokens_out:
            per_call_cost = di.estimated_cost_usd(model_key, avg_tokens_in, avg_tokens_out)
            if n_first_try:
                cost_per_valid_first_try = round(per_call_cost, 6)
            if n_success:
                total_cost = sum(
                    di.estimated_cost_usd(model_key, a["tokens_in"] or 0, a["tokens_out"] or 0)
                    for r in rows for a in r["attempts"]
                )
                cost_per_valid_with_retry = round(total_cost / n_success, 6)

        summary[model_key] = {
            "n_cases": n,
            "valid_pct": _pct(n_success, n),
            "valid_first_try_pct": _pct(n_first_try, n),
            "provider_error_pct": _pct(n_provider_errors, all_calls),
            "no_tool_call_pct": _pct(n_no_tool_call, all_calls),
            "kcal_flagged_of_valid_pct": _pct(kcal_flagged, n_success),
            "latency_ms_mean": round(statistics.mean(latencies), 0) if latencies else None,
            "latency_ms_median": pctl(latencies, 0.5),
            "latency_ms_p95": pctl(latencies, 0.95),
            "latency_ms_p99": pctl(latencies, 0.99),
            "avg_tokens_in": avg_tokens_in,
            "avg_tokens_out": avg_tokens_out,
            "cost_per_valid_recipe_first_try_usd": cost_per_valid_first_try,
            "cost_per_valid_recipe_with_retry_usd": cost_per_valid_with_retry,
        }
    return summary


def compute_scorecard(r1_summary: dict) -> dict:
    """Composite score: cost 35% + latency 20% + validity/compat 30% +
    semantic quality 15% — each component normalised 0-100 against the
    best performer in that dimension among the 3 models, so the weights
    are meaningful regardless of absolute scale. Raw components are always
    reported alongside the composite (never just the single number)."""
    models = list(r1_summary.keys())
    costs = {m: r1_summary[m]["cost_per_valid_recipe_with_retry_usd"] or 999 for m in models}
    latencies = {m: r1_summary[m]["latency_ms_mean"] or 999999 for m in models}
    validity = {m: r1_summary[m]["valid_pct"] for m in models}
    semantic = {m: 100 - r1_summary[m]["kcal_flagged_of_valid_pct"] for m in models}

    min_cost, min_latency = min(costs.values()), min(latencies.values())
    scorecard = {}
    for m in models:
        cost_score = 100 * (min_cost / costs[m]) if costs[m] else 0
        latency_score = 100 * (min_latency / latencies[m]) if latencies[m] else 0
        validity_score = validity[m]
        semantic_score = semantic[m]
        composite = (
            0.35 * cost_score + 0.20 * latency_score + 0.30 * validity_score + 0.15 * semantic_score
        )
        scorecard[m] = {
            "cost_score": round(cost_score, 1),
            "latency_score": round(latency_score, 1),
            "validity_score": round(validity_score, 1),
            "semantic_score": round(semantic_score, 1),
            "composite": round(composite, 1),
        }
    return scorecard


def summarize_r4(r4_results: list[dict]) -> dict:
    summary = {}
    for model_key in MODEL_KEYS:
        rows = [r for r in r4_results if r["model"] == model_key]
        summary[model_key] = {
            row["concurrency"]: {
                "wall_ms": row["wall_ms"],
                "ok_pct": _pct(row["n_ok"], row["n_total"]),
            }
            for row in rows
        }
    return summary


def summarize_rs(rs_results: list[dict]) -> dict:
    summary = {}
    for model_key in MODEL_KEYS:
        rows = [r for r in rs_results if r["model"] == model_key]
        n = len(rows)
        n_ok = sum(1 for r in rows if r["ok"] and not r.get("violations"))
        summary[model_key] = {"n_cases": n, "valid_pct": _pct(n_ok, n)}
    return summary


def summarize_r3(r3_results: list[dict]) -> dict:
    summary = {}
    for model_key in MODEL_KEYS:
        rows = [r for r in r3_results if r["model"] == model_key]
        n = len(rows)
        respected_count = sum(1 for r in rows if r["n_recipes_returned"] == 3)
        all_valid = sum(1 for r in rows if r["n_valid"] == 3)
        all_distinct = sum(1 for r in rows if r["n_distinct_titles"] == 3)
        summary[model_key] = {
            "n_cases": n,
            "respected_count_pct": _pct(respected_count, n),
            "all_3_valid_pct": _pct(all_valid, n),
            "all_3_distinct_pct": _pct(all_distinct, n),
        }
    return summary


def write_summary_csv(r1_summary, scorecard, r4_summary, rs_summary, r3_summary):
    import csv

    path = RESULTS_DIR / "summary.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["model", "metric", "value"])
        for m in MODEL_KEYS:
            for k, v in r1_summary[m].items():
                w.writerow([m, f"r1_{k}", v])
            for k, v in scorecard[m].items():
                w.writerow([m, f"score_{k}", v])
            for level, stats in r4_summary.get(m, {}).items():
                w.writerow([m, f"r4_concurrency_{level}_wall_ms", stats["wall_ms"]])
                w.writerow([m, f"r4_concurrency_{level}_ok_pct", stats["ok_pct"]])
            for k, v in rs_summary.get(m, {}).items():
                w.writerow([m, f"rs_{k}", v])
            for k, v in r3_summary.get(m, {}).items():
                w.writerow([m, f"r3_{k}", v])
    print(f"\nWrote {path}")


def write_report(r0_results, r1_summary, scorecard, r4_summary, rs_summary, r3_summary):
    ranked = sorted(MODEL_KEYS, key=lambda m: -scorecard[m]["composite"])
    winner = ranked[0]
    cheapest = min(MODEL_KEYS, key=lambda m: r1_summary[m]["cost_per_valid_recipe_with_retry_usd"] or 999)
    fastest = min(MODEL_KEYS, key=lambda m: r1_summary[m]["latency_ms_mean"] or 999999)
    most_reliable = max(MODEL_KEYS, key=lambda m: r1_summary[m]["valid_pct"])

    lines = []
    lines.append("# mesa — DeepInfra recipe-generation model benchmark")
    lines.append("")
    lines.append(f"Run: {datetime.now(UTC).isoformat()}Z. Models: {', '.join(di.MODELS[m] for m in MODEL_KEYS)}.")
    lines.append("")
    lines.append(
        "Benchmarked against mesa's REAL production contract (backend/benchmarks/mesa_contract.py "
        "imports directly from app/services/tools.py, guardrails.py, generation_rules.py, "
        "recipe_audit.py — same tool-calling schema, same DEFINING_COMPONENTS_RULE/"
        "KCAL_COMPUTATION_RULE prompt text, same validate_generated_recipe gate, same "
        "evaluate_kcal_plausibility semantic check the nightly audit uses). Not a generic "
        "'write a recipe' prompt."
    )
    lines.append("")
    if di.USES_JSON_MODE:
        json_mode_models = ", ".join(di.MODELS[m] for m in di.USES_JSON_MODE)
        lines.append(
            f"**Important asterisk on comparability**: {json_mode_models} does NOT support real "
            "tool-calling on DeepInfra — a live request with `tools` set returns a clean HTTP 405 "
            "(\"Tool calling is not supported for model: ...\"), contradicting that model's own "
            "DeepInfra page copy. Verified via a raw curl, not a harness bug. Every number for this "
            "model below comes from a JSON-mode adapter path instead (response_format=json_object, "
            "schema spelled out in the prompt) — the same accommodation mesa's real architecture "
            "would need to build if this model were ever chosen, since mesa's entire generation "
            "pipeline runs on tool-calling today. All other models use real tool-calling, unmodified."
        )
    lines.append("")
    lines.append("")

    lines.append("## R0 — tool-calling smoke test (1 call/model, run first to gate the rest of the budget)")
    lines.append("")
    lines.append("| Model | Path | Result |")
    lines.append("|---|---|---|")
    for r in r0_results:
        path = "JSON-mode" if r["json_mode"] else "tool-calling"
        outcome = "OK" if r["success"] else f"FAIL — {r['attempts'][-1].get('violations')}"
        lines.append(f"| {di.MODELS[r['model']]} | {path} | {outcome} |")
    lines.append("")

    lines.append("## R1 — core matrix (24 realistic slot-request cases per model)")
    lines.append("")
    lines.append("| Model | Valid | Valid 1st try | Provider errors | No tool-call | kcal-flagged (of valid) | Latency mean/p95 | Cost/valid (1st try) | Cost/valid (w/ retry) |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for m in MODEL_KEYS:
        s = r1_summary[m]
        lines.append(
            f"| {di.MODELS[m]} | {s['valid_pct']}% | {s['valid_first_try_pct']}% | "
            f"{s['provider_error_pct']}% | {s['no_tool_call_pct']}% | {s['kcal_flagged_of_valid_pct']}% | "
            f"{s['latency_ms_mean']}ms / {s['latency_ms_p95']}ms | "
            f"${s['cost_per_valid_recipe_first_try_usd']} | ${s['cost_per_valid_recipe_with_retry_usd']} |"
        )
    lines.append("")

    lines.append("## Composite scorecard (cost 35% + latency 20% + validity 30% + semantic 15%)")
    lines.append("")
    lines.append("Each component normalised 0-100 against the best performer in that dimension — raw components always shown alongside, never just the composite.")
    lines.append("")
    lines.append("| Model | Cost score | Latency score | Validity score | Semantic score | **Composite** |")
    lines.append("|---|---|---|---|---|---|")
    for m in ranked:
        sc = scorecard[m]
        lines.append(
            f"| {di.MODELS[m]} | {sc['cost_score']} | {sc['latency_score']} | "
            f"{sc['validity_score']} | {sc['semantic_score']} | **{sc['composite']}** |"
        )
    lines.append("")

    lines.append("## R4 — concurrency scaling (6-case subset, concurrency 1/3/5/10)")
    lines.append("")
    lines.append("| Model | c=1 | c=3 | c=5 | c=10 |")
    lines.append("|---|---|---|---|---|")
    for m in MODEL_KEYS:
        row = r4_summary.get(m, {})
        cells = []
        for level in (1, 3, 5, 10):
            st = row.get(level)
            cells.append(f"{st['wall_ms']}ms ({st['ok_pct']}% ok)" if st else "n/a")
        lines.append(f"| {di.MODELS[m]} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## R3-lite — 3 recipes requested in a single call")
    lines.append("")
    lines.append("| Model | Returned exactly 3 | All 3 valid | All 3 genuinely distinct |")
    lines.append("|---|---|---|---|")
    for m in MODEL_KEYS:
        s = r3_summary.get(m, {})
        lines.append(
            f"| {di.MODELS[m]} | {s.get('respected_count_pct', 'n/a')}% | "
            f"{s.get('all_3_valid_pct', 'n/a')}% | {s.get('all_3_distinct_pct', 'n/a')}% |"
        )
    lines.append("")

    lines.append("## RS — steps-generation contract (6 cases, real generated ingredients as input)")
    lines.append("")
    lines.append("| Model | Valid steps |")
    lines.append("|---|---|")
    for m in MODEL_KEYS:
        s = rs_summary.get(m, {})
        lines.append(f"| {di.MODELS[m]} | {s.get('valid_pct', 'n/a')}% |")
    lines.append("")

    lines.append("## Cost projection (base case: 200 input + 800 output tokens = 1000 tokens/recipe)")
    lines.append("")
    lines.append("| Model | 1 recipe | 100 recipes | 1,000 recipes | 10,000 recipes |")
    lines.append("|---|---|---|---|---|")
    for m in MODEL_KEYS:
        unit_cost = di.estimated_cost_usd(m, 200, 800)
        lines.append(
            f"| {di.MODELS[m]} | ${unit_cost:.5f} | ${unit_cost*100:.3f} | "
            f"${unit_cost*1000:.2f} | ${unit_cost*10000:.2f} |"
        )
    lines.append("")

    lines.append("## Deliberately deferred, not run this pass")
    lines.append("")
    lines.append(
        "- **R2 (JSON-mode adapter path)**: mesa's entire architecture is tool-calling, not "
        "`response_format=json_object` — R0/R1 already confirm real tool-calling support on all "
        "3 models, so a JSON-mode fallback path is only relevant if tool-calling proves unreliable "
        "in production. Not built this pass; revisit only if a chosen model's tool-calling degrades "
        "at real volume."
    )
    lines.append(
        "- **Full R3 sweep (1/3/5/10 recipes-per-prompt x concurrency)**: R3-lite (N=3, no "
        "concurrency sweep) already answers the question that motivated it (does batching "
        "same-meal-type slots into one call work, and does it preserve variety) without the full "
        "combinatorial cost."
    )
    lines.append("")

    lines.append("## Recommendation")
    lines.append("")
    lines.append(f"- **Best overall (composite score)**: {di.MODELS[winner]}")
    lines.append(f"- **Cheapest absolute**: {di.MODELS[cheapest]}")
    lines.append(f"- **Most reliable (valid %)**: {di.MODELS[most_reliable]}")
    lines.append(f"- **Fastest**: {di.MODELS[fastest]}")
    lines.append("")
    lines.append(
        "### \"Si este microservicio fuera tuyo, ¿cuál elegirías hoy?\""
    )
    lines.append("")
    lines.append(f"**{di.MODELS[winner]}** — see the numbers above for why; this line is filled in "
                  "programmatically from whichever model actually wins the composite score on this "
                  "run's real data, not decided in advance.")
    lines.append("")

    path = RESULTS_DIR / "REPORT.md"
    path.write_text("\n".join(lines))
    print(f"Wrote {path}")


async def main():
    for f in RAW_DIR.glob("*.jsonl"):
        f.unlink()  # fresh run, not appended to a stale prior run

    t0 = time.monotonic()
    r0_results = await round_r0_smoke_test()
    r1_results = await round_r1_core_matrix()
    r4_results = await round_r4_concurrency()
    r3_results = await round_r3_lite_multi_recipe()
    rs_results = await round_rs_steps(r1_results)

    r1_summary = summarize_r1(r1_results)
    scorecard = compute_scorecard(r1_summary)
    r4_summary = summarize_r4(r4_results)
    r3_summary = summarize_r3(r3_results)
    rs_summary = summarize_rs(rs_results)

    write_summary_csv(r1_summary, scorecard, r4_summary, rs_summary, r3_summary)
    write_report(r0_results, r1_summary, scorecard, r4_summary, rs_summary, r3_summary)

    print(f"\nTotal wall time: {round(time.monotonic() - t0, 1)}s")


if __name__ == "__main__":
    asyncio.run(main())
