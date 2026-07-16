"""OpenRouter vs DeepInfra, same 2 models, same 24 fixtures.py cases, same
mesa_contract validators — answers a live question (2026-07-16): production
traffic through openrouter_paid_completion is seeing far more validation
retries than the original benchmark's 91.7%/95.8% (measured on DeepInfra
only) would predict. Is that the MODEL or the PROVIDER serving it?

Run with: backend/venv/bin/python -m benchmarks.provider_showdown
(from backend/, with both DEEPINFRA_API_KEY and OPENROUTER_API_KEY set)
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import deepinfra_client as di  # noqa: E402
import fixtures  # noqa: E402
import mesa_contract as mc  # noqa: E402
import openrouter_client as orc  # noqa: E402

MAX_ATTEMPTS = 2
CONCURRENCY = 5

PROVIDERS = {"deepinfra": di, "openrouter": orc}
MODEL_KEYS = ["mistral-small-24b", "phi-4"]


async def _run_one(provider_name: str, client, model_key: str, case: fixtures.Case) -> dict:
    system_prompt = mc.build_json_mode_system_prompt(case)
    user_prompt = f"Generate one {case.meal_type} recipe now."
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    attempts = []
    final = None
    for attempt_n in range(1, MAX_ATTEMPTS + 1):
        try:
            result = await client.chat_completion(model_key, messages, None, json_mode=True)
            outcome = {
                "attempt": attempt_n,
                "ok": result["ok"],
                "error": result["error"],
                "latency_ms": result["latency_ms"],
            }
            if not result["ok"]:
                outcome["violations"] = [f"provider/network error: {result['error']}"]
                attempts.append(outcome)
                continue

            recipe = di.extract_json_content(result)
            if recipe is None:
                outcome["violations"] = ["model did not return parseable JSON"]
                outcome["raw_content"] = (result["content"] or "")[:300]
                attempts.append(outcome)
                continue

            violations = mc.validate_against_mesa_contract(recipe, case)
            outcome["violations"] = violations
            attempts.append(outcome)
            if not violations:
                final = {"attempt_n": attempt_n, "recipe": recipe}
                break
        except Exception as exc:  # noqa: BLE001 — harness resilience
            attempts.append({
                "attempt": attempt_n, "ok": False, "error": repr(exc),
                "latency_ms": 0, "violations": [f"harness error: {exc!r}"],
            })
            continue

    return {
        "provider": provider_name,
        "model": model_key,
        "case": case.label,
        "success": final is not None,
        "success_first_attempt": final is not None and final["attempt_n"] == 1,
        "n_attempts": len(attempts),
        "total_latency_ms": sum(a["latency_ms"] for a in attempts),
        "violations_by_attempt": [a["violations"] for a in attempts],
    }


async def _run_bounded(sem, *args):
    async with sem:
        return await _run_one(*args)


async def main():
    sem = asyncio.Semaphore(CONCURRENCY)
    all_results = []

    for model_key in MODEL_KEYS:
        for provider_name, client in PROVIDERS.items():
            print(f"\n=== {provider_name} / {model_key}: {len(fixtures.CASES)} cases ===")
            start = time.monotonic()
            tasks = [_run_bounded(sem, provider_name, client, model_key, c) for c in fixtures.CASES]
            results = await asyncio.gather(*tasks)
            elapsed = time.monotonic() - start
            all_results.extend(results)

            n = len(results)
            successes = sum(r["success"] for r in results)
            first_try = sum(r["success_first_attempt"] for r in results)
            avg_latency = sum(r["total_latency_ms"] for r in results) / n
            print(f"  valid: {successes}/{n} ({successes/n*100:.1f}%)  "
                  f"first-try: {first_try}/{n} ({first_try/n*100:.1f}%)  "
                  f"avg total latency: {avg_latency:.0f}ms  wall-clock: {elapsed:.1f}s")

            failures = [r for r in results if not r["success"]]
            if failures:
                print("  failures:")
                for r in failures:
                    last_violations = r["violations_by_attempt"][-1] if r["violations_by_attempt"] else []
                    print(f"    - {r['case']}: {last_violations}")

    print("\n=== SUMMARY (model x provider) ===")
    for model_key in MODEL_KEYS:
        for provider_name in PROVIDERS:
            subset = [r for r in all_results if r["model"] == model_key and r["provider"] == provider_name]
            n = len(subset)
            successes = sum(r["success"] for r in subset)
            avg_latency = sum(r["total_latency_ms"] for r in subset) / n
            print(f"  {model_key:20s} {provider_name:12s} valid={successes}/{n} ({successes/n*100:5.1f}%)  avg_latency={avg_latency:.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())
