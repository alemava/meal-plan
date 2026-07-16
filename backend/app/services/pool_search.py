from app.core import db
from app.services import cloudflare, deepinfra, history
from app.services.profile import UserProfile

# Cosine distance from pgvector's `<=>` operator (0 = identical, 2 = opposite).
# similarity = 1 - distance. Conservative starting point — the plan is explicit
# this must be tuned against real usage data, not trusted as-is.
SIMILARITY_THRESHOLD = 0.70
CANDIDATE_POOL_SIZE = 20


def build_query_text(profile: UserProfile, meal_type: str, any_cuisine: bool = False) -> str:
    if any_cuisine:
        return f"A {meal_type} recipe."
    cuisines = ", ".join(profile.cuisines) or "any cuisine"
    return f"A {meal_type} recipe from {cuisines} cuisine."


async def _get_query_vector_literal(query_text: str) -> str | None:
    """asyncpg returns `vector` columns as their literal text form (the same
    `[1,2,...]` string `$n::vector` accepts), so a cache hit needs no codec
    work. Query texts are few and highly repetitive across users/profiles, so
    after the first real search for a given (meal_type, cuisine-set) shape,
    every later one skips a real embedding call entirely — both a cost win
    and what makes the degraded-pool ladder (search_recipe_pool below) usable
    even when the embedding provider itself is down.

    DeepInfra, not Cloudflare, since 2026-07-16 — deliberately the ONLY
    embedding provider (no automatic fallback to Cloudflare on failure):
    verified live that the same text embedded by the two providers is only
    0.976 cosine-similar, not 1.0 — enough noise to corrupt match decisions
    right at SIMILARITY_THRESHOLD if the pool ever mixed vectors from both.
    A failure here behaves exactly as a Cloudflare failure used to: no
    match, never a silent substitute model."""
    cached = await db.pool().fetchval(
        "SELECT embedding::text FROM query_embedding_cache WHERE query_text = $1", query_text
    )
    if cached:
        return cached
    try:
        vector = await deepinfra.embed_text(query_text)
    except Exception:
        return None
    literal = cloudflare.vector_literal(vector)
    await db.pool().execute(
        "INSERT INTO query_embedding_cache (query_text, embedding) VALUES ($1, $2::vector) "
        "ON CONFLICT (query_text) DO NOTHING",
        query_text,
        literal,
    )
    return literal


async def search_recipe_pool(
    profile: UserProfile,
    meal_type: str,
    exclude_ids: set[str] | None = None,
    used_cuisines: list[str] | None = None,
    *,
    relax_suggested: bool = False,
    min_similarity: float | None = None,
    any_cuisine: bool = False,
    include_stubs: bool = True,
) -> dict | None:
    """ADR 1 — pgvector similarity search over the pool, then hard filters in
    code, then the similarity threshold applied to whatever survives.

    The keyword-only params (all default to today's exact behaviour) exist
    for the degraded-mode fill ladder in generate_recipes.py — each one
    relaxes a single dimension so a caller can widen the search step by step
    instead of giving up when both AI providers are down:
      - relax_suggested: drop the 28-day "recently shown" exclusion (the
        60-day "actually used" exclusion and allergen/dislike filters are
        NEVER relaxed, on any path).
      - min_similarity: override SIMILARITY_THRESHOLD.
      - any_cuisine: drop the hard preferred-cuisine filter.
      - include_stubs: a stub needs a live AI call to expand — poison for a
        providers-down fallback, so the ladder always passes False here.

    NOT implemented here (no data source exists yet, would be a fake check):
    "recipes whose required_ingredient_tags the user can mostly satisfy" needs
    a pantry data model that doesn't exist pre-onboarding — same gap as the
    documented-deferred country-availability filter. Skipped honestly rather
    than faked.
    """
    query_text = build_query_text(profile, meal_type, any_cuisine=any_cuisine)
    query_vector_literal = await _get_query_vector_literal(query_text)
    if query_vector_literal is None:
        # Cloudflare unavailable and no cache hit -> no pool match, not a
        # crash. ADR 2's own fallback ("no qualifying match -> both fresh")
        # already covers exactly this case; the caller doesn't need to know why.
        return None

    recently_used = await history.get_recently_used_recipe_ids(profile.user_id)
    excluded = recently_used | (exclude_ids or set())
    if not relax_suggested:
        excluded |= await history.get_recently_suggested_recipe_ids(profile.user_id)

    statuses = ["complete", "partial"] + (["stub"] if include_stubs else [])
    # Exclusion moved into SQL (not a Python post-filter) so LIMIT applies to
    # eligible rows: with a heavily-excluded pool (repeat testing, a thin
    # cuisine), the top-N by raw similarity can be 100% excluded ids while a
    # qualifying candidate sits just past the old cutoff.
    rows = await db.pool().fetch(
        """
        SELECT id, title, brief_description, cuisine, main_protein, ingredients,
               image_url, status, source, base_serves, time, kcal,
               embedding <=> $1::vector AS distance
        FROM recipes
        WHERE status = ANY($2::text[]) AND embedding IS NOT NULL
          AND NOT (id::text = ANY($3::text[]))
        ORDER BY embedding <=> $1::vector
        LIMIT $4
        """,
        query_vector_literal,
        statuses,
        list(excluded),
        CANDIDATE_POOL_SIZE,
    )

    threshold = min_similarity if min_similarity is not None else SIMILARITY_THRESHOLD
    preferred_cuisines = {c.lower() for c in profile.cuisines}
    used_cuisines_lower = {c.lower() for c in (used_cuisines or []) if c}
    # Soft preference, not a hard filter: skip a cuisine already used elsewhere
    # in this batch, but remember the first otherwise-valid match as a
    # fallback in case every qualifying candidate shares that same cuisine —
    # better to return a repeat than nothing (caller falls back to fresh
    # generation on None, which isn't the outcome we want here).
    fallback = None
    for row in rows:
        row_cuisine = (row["cuisine"] or "").lower()
        if not any_cuisine and preferred_cuisines and row_cuisine not in preferred_cuisines:
            continue
        if row["main_protein"] and row["main_protein"] in profile.dislikes:
            continue
        if _has_allergen_conflict(row["ingredients"], profile.allergies):
            continue
        if (1 - row["distance"]) < threshold:
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
