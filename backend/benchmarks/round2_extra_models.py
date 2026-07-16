"""Round 2 (2026-07-15, user-requested): before committing to Mistral Small
24B (round 1's winner), check whether a genuinely cheaper/different
DeepInfra candidate beats it. Scoped to R0 (tool-calling check) + R1 (core
matrix) only, for just the 2 new candidates — reuses round 1's exact
Mistral Small 24B numbers for the comparison rather than re-spending on
already-known results.

Run from backend/: venv/bin/python -m benchmarks.round2_extra_models
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import deepinfra_client as di  # noqa: E402
import fixtures  # noqa: E402
import runner  # noqa: E402

NEW_MODEL_KEYS = ["mistral-nemo-12b", "phi-4"]


async def main():
    print("=== R0: tool-calling smoke test (round 2 candidates) ===")
    case = fixtures.CASES[0]
    for model_key in NEW_MODEL_KEYS:
        record = await runner._run_one_recipe_case(model_key, case, "round2_r0_smoke")
        status = "OK" if record["success"] else "FAIL"
        path = "JSON-mode" if model_key in di.USES_JSON_MODE else "tool-calling"
        print(f"  {model_key:20s} [{path}] {status}  attempts={record['n_attempts']}")
        if not record["success"]:
            print(f"    last violation(s): {record['attempts'][-1].get('violations')}")

    print(f"\n=== R1: core matrix ({len(fixtures.CASES)} cases) — round 2 candidates ===")
    r1_results = []
    for model_key in NEW_MODEL_KEYS:
        print(f"  running {model_key}...")
        for case in fixtures.CASES:
            record = await runner._run_one_recipe_case(model_key, case, "round2_r1_core")
            r1_results.append(record)
            print("." if record["success"] else "X", end="", flush=True)
        print()

    r1_summary = runner.summarize_r1(r1_results)

    # Pull round 1's already-computed Mistral Small 24B numbers back in for
    # a like-for-like comparison table, without re-spending on it.
    import json
    mistral_small_rows = []
    with open(runner.RAW_DIR / "r1_core.jsonl") as f:
        for line in f:
            r = json.loads(line)
            if r["model"] == "mistral-small-24b":
                mistral_small_rows.append(r)
    if mistral_small_rows:
        r1_summary["mistral-small-24b"] = runner.summarize_r1(mistral_small_rows)["mistral-small-24b"]

    all_keys = [*NEW_MODEL_KEYS, "mistral-small-24b"] if mistral_small_rows else NEW_MODEL_KEYS
    print("\n=== Comparison table ===")
    print(f"{'model':22s} {'valid%':>8s} {'valid1st%':>10s} {'latency_mean':>13s} {'cost/valid(retry)':>18s}")
    for m in all_keys:
        s = r1_summary[m]
        print(f"{m:22s} {s['valid_pct']:>7.1f}% {s['valid_first_try_pct']:>9.1f}% "
              f"{str(s['latency_ms_mean'])+'ms':>13s} ${s['cost_per_valid_recipe_with_retry_usd']}")

    Path(runner.RESULTS_DIR / "round2_summary.txt").write_text(
        "\n".join(
            f"{m}: {r1_summary[m]}" for m in all_keys
        )
    )
    print(f"\nWrote {runner.RESULTS_DIR / 'round2_summary.txt'}")


if __name__ == "__main__":
    asyncio.run(main())
