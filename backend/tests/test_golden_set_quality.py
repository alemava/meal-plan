"""Golden-set quality gate — production-readiness Part 3 (2026-07-15).

The one rigorous, unconfounded comparison between Groq and OpenRouter.
Every other quality signal in this project (recipe_audit.py's nightly
findings, provider_quality.py's scorecard) reads ORGANIC traffic — and
ai_client.chat_completion() tries Groq first, only falling through to
OpenRouter when Groq already failed. That makes OpenRouter's organic sample
systematically "Groq's hard cases, retried on a different model," not a
fair draw from the same request distribution. This generates a small fixed
matrix on EACH provider independently via ai_client.groq_only_completion/
openrouter_only_completion (which never fall back to each other), so both
see the exact same requests.

Manual gate, not CI (none exists in this repo) — makes real provider calls
and spends real quota. Run explicitly:
    pytest -m golden_set -v -s
"""

import pytest

from app.services import ai_client, recipe_audit
from app.services.fresh_generation import _SUBMIT_ONLY_SCHEMAS, _build_system_prompt
from app.services.guardrails import GeneratedRecipeInvalid, validate_ingredient_shape, validate_ingredient_units
from app.services.profile import UserProfile

pytestmark = pytest.mark.golden_set

# Small and cheap on purpose — scale up only if a first run's signal is
# ambiguous (see the plan). 3 cuisines x 2 meal types x 2 providers = 12
# real generations total.
CUISINES = ["italian", "mexican", "thai"]
MEAL_TYPES = ["dinner", "breakfast"]

DETERMINISTIC_PASS_RATE_THRESHOLD = 0.90


def _test_profile(cuisine: str) -> UserProfile:
    return UserProfile(
        user_id="00000000-0000-0000-0000-000000000000",
        cuisines=[cuisine],
        allergies=[],
        dislikes=[],
        household_size=2,
        skill="intermediate",
    )


async def _generate_and_evaluate(completion_fn, cuisine: str, meal_type: str) -> dict:
    """One forced-provider generation, evaluated with the SAME validators
    generation itself uses (validate_ingredient_shape/validate_ingredient_
    units — Part B's at-source guardrails) plus the shared kcal heuristic
    (recipe_audit.evaluate_kcal_plausibility) — never persisted, this is an
    isolated quality probe, not a write to the real pool."""
    profile = _test_profile(cuisine)
    system_prompt = _build_system_prompt(profile, meal_type, [], None, None, None, None)

    result: dict = {"cuisine": cuisine, "meal_type": meal_type}
    try:
        recipe, provider = await ai_client.run_tool_use_loop(
            system_prompt,
            f"Generate one {meal_type} recipe now.",
            _SUBMIT_ONLY_SCHEMAS,
            dispatch={},
            purpose="generate_recipes",
            completion_fn=completion_fn,
        )
    except ai_client.AIProviderExhausted as exc:
        result["provider"] = "unknown"
        result["title"] = None
        result["deterministic_ok"] = False
        result["deterministic_error"] = f"AIProviderExhausted: {exc}"
        result["kcal_findings"] = []
        return result

    result["provider"] = provider
    result["title"] = recipe.get("title")
    try:
        validate_ingredient_shape(recipe["ingredients"])
        validate_ingredient_units(recipe["ingredients"])
        result["deterministic_ok"] = True
    except GeneratedRecipeInvalid as exc:
        result["deterministic_ok"] = False
        result["deterministic_error"] = str(exc)

    result["kcal_findings"] = recipe_audit.evaluate_kcal_plausibility(
        recipe.get("ingredients"), recipe.get("kcal"), base_serves=2
    )
    return result


@pytest.mark.asyncio
async def test_golden_set_quality(db_pool):
    matrix = [(cuisine, meal_type) for cuisine in CUISINES for meal_type in MEAL_TYPES]

    results_by_provider = {"groq": [], "openrouter": []}
    for cuisine, meal_type in matrix:
        results_by_provider["groq"].append(
            await _generate_and_evaluate(ai_client.groq_only_completion, cuisine, meal_type)
        )
        results_by_provider["openrouter"].append(
            await _generate_and_evaluate(ai_client.openrouter_only_completion, cuisine, meal_type)
        )

    failures = []
    for label, results in results_by_provider.items():
        total = len(results)
        deterministic_clean = sum(1 for r in results if r["deterministic_ok"])
        pass_rate = deterministic_clean / total if total else 0.0
        kcal_flag_count = sum(1 for r in results if r["kcal_findings"])

        print(f"\n=== {label}: {deterministic_clean}/{total} deterministic-clean ({pass_rate:.0%}) ===")
        for r in results:
            marker = "OK  " if r["deterministic_ok"] else "FAIL"
            print(f"  {marker} {r['cuisine']}/{r['meal_type']}: {r['title']}")
            if not r["deterministic_ok"]:
                print(f"       -> {r['deterministic_error']}")
            if r["kcal_findings"]:
                print(f"       kcal (reported, not asserted): {r['kcal_findings']}")
        print(f"  kcal/heuristic flags: {kcal_flag_count}/{total} (reported only, not a gate)")

        # Only the deterministic tier gates — the heuristic/semantic tiers
        # are reported for visibility. Asserting on kcal here would make
        # this test flaky on the estimator's own known imprecision, and
        # there's no semantic (missing_defining_ingredient) check at all in
        # this matrix — that check needs a live Cloudflare call per recipe
        # and has a measured ~33% precision, not something to gate a
        # pass/fail test on.
        if pass_rate < DETERMINISTIC_PASS_RATE_THRESHOLD:
            failures.append(
                f"{label}: {pass_rate:.0%} deterministic pass rate, below "
                f"{DETERMINISTIC_PASS_RATE_THRESHOLD:.0%} threshold"
            )

    assert not failures, "; ".join(failures)
