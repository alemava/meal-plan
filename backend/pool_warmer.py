"""ADR 4 — nightly pool warming. Generates a handful of speculative 'stub'
recipes (title + description + image, no ingredients/steps yet) using only
free Cloudflare models, so real users' hybrid searches (step 15) have more
to find. Stubs only cost real AI-provider usage when step 16 expands one on
demand — this script itself is zero-Anthropic/OpenRouter-cost by design.

Triggered nightly by Cloud Scheduler hitting POST /api/admin/pool-warm (see
app/api/admin.py). Can also run standalone for local testing:
    python pool_warmer.py
"""

import json
import random

from app.core import db
from app.services import cloudflare, deepinfra, image_chain, provider_quota, recipe_audit
from app.services.generation_rules import DEFINING_COMPONENTS_RULE, DISH_AUTHENTICITY_RULE
from app.services.profile import DEFAULT_CUISINES
from app.services.stub_expansion import expand_stub

STUBS_PER_NIGHT = 8

# Optional, quota-gated top-up beyond plain stub creation (2026-07-14) — see
# _expand_some_stubs below. Deliberately modest (less than STUBS_PER_NIGHT,
# not a doubling of nightly spend) since this is real Groq/OpenRouter spend,
# unlike stub creation itself which stays free/Cloudflare-only.
MAX_EXPANSIONS_PER_NIGHT = 5
EXPANSION_QUOTA_THRESHOLD_PERCENT = 50

SYSTEM_PROMPT = (
    "You invent simple, appealing recipe ideas for a meal-planning app. "
    f"{DISH_AUTHENTICITY_RULE} {DEFINING_COMPONENTS_RULE} "
    "Respond with ONLY a JSON object: "
    '{"title": "...", "brief_description": "...", "main_protein": "...", "image_prompt": "..."}. '
    "image_prompt should describe the finished plated dish visually (colours, textures, "
    "garnish, vessel) for an image generator, ending with "
    '", close-up food photography, warm natural light, appetizing".'
)


async def _cuisine_weights() -> dict[str, int]:
    """Step 32 — bias stub generation toward cuisines real users actually
    want, proportionally (2026-07-14): every one of profile.py's
    DEFAULT_CUISINES gets a floor of 1 vote — guarantees coverage for users
    who haven't been through onboarding yet — and real profiles.cuisines
    counts stack on top. Self-scaling: as the real user base grows, the
    fixed floor naturally matters less relative to real signal, with no
    hardcoded user-count threshold needed.

    Previously this returned cuisines sorted by popularity, but the caller
    picked with `random.choice()` — a uniform pick that silently discarded
    the ordering, so the "proportional" bias never actually applied. Fixed
    by returning real weights and picking with `random.choices(weights=...)`
    instead (see run_pool_warmer)."""
    weights = dict.fromkeys(DEFAULT_CUISINES, 1)
    rows = await db.pool().fetch("SELECT cuisines FROM profiles WHERE cuisines IS NOT NULL")
    for row in rows:
        for cuisine in row["cuisines"] or []:
            weights[cuisine] = weights.get(cuisine, 0) + 1
    return weights


async def _generate_one_stub(cuisine: str, meal_type: str) -> None:
    # meal_type was previously hardcoded to "dinner" in both prompts below —
    # a real bug (not by design): every stub ever created by this warmer was
    # dinner-framed, silently under-serving breakfast/lunch pool matches.
    # No per-user meal-type-preference signal exists anywhere in `profiles`
    # (unlike cuisine), so a plain round-robin (see run_pool_warmer) is the
    # honest choice here rather than inventing a weighting scheme to guess.
    user_prompt = f"Invent a {cuisine} {meal_type} recipe idea."
    raw = await provider_quota.call_cloudflare_text_with_quota(
        "pool_warmer_stub", SYSTEM_PROMPT, user_prompt
    )
    idea = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])

    row = await db.pool().fetchrow(
        """
        INSERT INTO recipes (title, cuisine, main_protein, brief_description, image_prompt, status, source, verified)
        VALUES ($1, $2, $3, $4, $5, 'stub', 'cloudflare', false)
        RETURNING id
        """,
        idea["title"],
        cuisine,
        idea.get("main_protein"),
        idea["brief_description"],
        idea["image_prompt"],
    )

    vector = await deepinfra.embed_text(f"{idea['title']}. {idea['brief_description']}")
    await db.pool().execute(
        "UPDATE recipes SET embedding = $1::vector WHERE id = $2",
        cloudflare.vector_literal(vector),
        row["id"],
    )

    # Batch work, not a live request — queueing on failure is always fine here (step 35).
    image_url = await image_chain.generate_and_upload_image(str(row["id"]), idea["image_prompt"])
    if image_url:
        await db.pool().execute(
            "UPDATE recipes SET image_url = $1 WHERE id = $2", image_url, row["id"]
        )


_MEAL_TYPES = ["breakfast", "lunch", "dinner"]


async def _expand_some_stubs(weights: dict[str, int]) -> tuple[int, list[str]]:
    """Quota-aware top-up beyond plain stub creation (2026-07-14) — this is
    the part that actually reduces tomorrow's live API calls: a stub alone
    still needs a real expand_stub() call (real Groq/OpenRouter spend)
    before it's servable, so pre-expanding some of the backlog overnight
    means more slots can be served entirely from the pool the next day (see
    generate_recipes.py's proactive-second-pool-search change). Gated on
    leftover quota so this never competes with live daytime traffic for the
    same daily budget. Since pool_warmer runs early in the UTC day, usage
    will typically already be low almost by construction — this threshold
    is a safety valve for the rare case it isn't, not a real forecast."""
    if (
        await provider_quota.get_percent_used("openrouter") >= EXPANSION_QUOTA_THRESHOLD_PERCENT
        or await provider_quota.get_percent_used("groq") >= EXPANSION_QUOTA_THRESHOLD_PERCENT
    ):
        return 0, []

    candidates = await db.pool().fetch(
        "SELECT * FROM recipes WHERE status = 'stub' ORDER BY created_at ASC LIMIT 50"
    )
    # Prefer expanding popular-cuisine stubs first — reuses the same weights
    # as tonight's stub creation, doubling down on exactly the cuisines the
    # proactive-second-pool-search change benefits most from having
    # pre-expanded, ready-to-serve options for.
    ranked = sorted(candidates, key=lambda r: weights.get(r["cuisine"], 0), reverse=True)

    expanded = 0
    errors: list[str] = []
    for stub in ranked[:MAX_EXPANSIONS_PER_NIGHT]:
        try:
            await expand_stub(dict(stub))
            expanded += 1
        except Exception as exc:  # noqa: BLE001 — one bad expansion shouldn't kill the batch
            # str(exc) alone is "" for exceptions raised with no message
            # (seen live: AIProviderExhausted) — include the type so the
            # report is never a silently empty string.
            errors.append(f"{type(exc).__name__}: {exc}")
    return expanded, errors


async def run_pool_warmer() -> dict:
    weights = await _cuisine_weights()
    population, counts = list(weights), list(weights.values())
    created = 0
    errors = []

    for i in range(STUBS_PER_NIGHT):
        cuisine = random.choices(population=population, weights=counts, k=1)[0]
        meal_type = _MEAL_TYPES[i % len(_MEAL_TYPES)]
        try:
            await _generate_one_stub(cuisine, meal_type)
            created += 1
        except Exception as exc:  # noqa: BLE001 — one bad stub shouldn't kill the batch
            errors.append(f"{type(exc).__name__}: {exc}")

    expanded, expansion_errors = await _expand_some_stubs(weights)

    # Part C (2026-07-14) — runs last, exactly when new content (this run's
    # stubs/expansions) exists to check. Never blocks stub creation: if the
    # audit itself errors, the night's stubs are already committed either way.
    try:
        audit = await recipe_audit.run_recipe_audit()
    except Exception as exc:  # noqa: BLE001 — audit failure shouldn't fail the whole warmer run
        audit = {"audit_error": str(exc)}

    return {
        "requested": STUBS_PER_NIGHT,
        "created": created,
        "errors": errors,
        "expanded": expanded,
        "expansion_errors": expansion_errors,
        **audit,
    }


async def _main() -> None:
    await db.connect()
    try:
        result = await run_pool_warmer()
        print(result)
    finally:
        await db.disconnect()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
