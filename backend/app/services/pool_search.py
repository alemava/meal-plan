from app.core import db
from app.services import cloudflare, history
from app.services.profile import UserProfile

# Cosine distance from pgvector's `<=>` operator (0 = identical, 2 = opposite).
# similarity = 1 - distance. Conservative starting point — the plan is explicit
# this must be tuned against real usage data, not trusted as-is.
SIMILARITY_THRESHOLD = 0.70
CANDIDATE_POOL_SIZE = 20


def build_query_text(profile: UserProfile, meal_type: str) -> str:
    cuisines = ", ".join(profile.cuisines) or "any cuisine"
    return f"A {meal_type} recipe from {cuisines} cuisine."


async def search_recipe_pool(
    profile: UserProfile,
    meal_type: str,
    exclude_ids: set[str] | None = None,
    used_cuisines: list[str] | None = None,
) -> dict | None:
    """ADR 1 — pgvector similarity search over the pool, then hard filters in
    code, then the similarity threshold applied to whatever survives.

    NOT implemented here (no data source exists yet, would be a fake check):
    "recipes whose required_ingredient_tags the user can mostly satisfy" needs
    a pantry data model that doesn't exist pre-onboarding — same gap as the
    documented-deferred country-availability filter. Skipped honestly rather
    than faked.
    """
    query_text = build_query_text(profile, meal_type)
    try:
        query_vector = await cloudflare.embed_text(query_text)
    except Exception:
        # Cloudflare unavailable (e.g. rate-limited) -> no pool match, not a
        # crash. ADR 2's own fallback ("no qualifying match -> both fresh")
        # already covers exactly this case; the caller doesn't need to know why.
        return None

    recently_used = await history.get_recently_used_recipe_ids(profile.user_id)
    recently_suggested = await history.get_recently_suggested_recipe_ids(profile.user_id)
    excluded = recently_used | recently_suggested | (exclude_ids or set())

    rows = await db.pool().fetch(
        """
        SELECT id, title, brief_description, cuisine, main_protein, ingredients,
               image_url, status, source, base_serves, time, kcal,
               embedding <=> $1::vector AS distance
        FROM recipes
        WHERE status IN ('complete', 'partial', 'stub') AND embedding IS NOT NULL
        ORDER BY embedding <=> $1::vector
        LIMIT $2
        """,
        cloudflare.vector_literal(query_vector),
        CANDIDATE_POOL_SIZE,
    )

    preferred_cuisines = {c.lower() for c in profile.cuisines}
    used_cuisines_lower = {c.lower() for c in (used_cuisines or []) if c}
    # Soft preference, not a hard filter: skip a cuisine already used elsewhere
    # in this batch, but remember the first otherwise-valid match as a
    # fallback in case every qualifying candidate shares that same cuisine —
    # better to return a repeat than nothing (caller falls back to fresh
    # generation on None, which isn't the outcome we want here).
    fallback = None
    for row in rows:
        if str(row["id"]) in excluded:
            continue
        row_cuisine = (row["cuisine"] or "").lower()
        if preferred_cuisines and row_cuisine not in preferred_cuisines:
            continue
        if row["main_protein"] and row["main_protein"] in profile.dislikes:
            continue
        if _has_allergen_conflict(row["ingredients"], profile.allergies):
            continue
        if (1 - row["distance"]) < SIMILARITY_THRESHOLD:
            continue
        if row_cuisine in used_cuisines_lower:
            if fallback is None:
                fallback = dict(row)
            continue
        return dict(row)

    return fallback


def _has_allergen_conflict(ingredients: list[dict] | None, allergies: list[str]) -> bool:
    if not ingredients or not allergies:
        return False
    for ingredient in ingredients:
        if set(ingredient.get("allergen_tags", [])) & set(allergies):
            return True
    return False
