from app.core import db
from app.services import cloudflare, deepinfra, history
from app.services.guardrails import parse_time_minutes
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
    avoid_titles: set[str] | None = None,
    relax_suggested: bool = False,
    min_similarity: float | None = None,
    any_cuisine: bool = False,
    include_stubs: bool = True,
    max_time_minutes: int | None = None,
    category_keywords: list[str] | None = None,
) -> dict | None:
    """ADR 1 — pgvector similarity search over the pool, then hard filters in
    code, then the similarity threshold applied to whatever survives.

    The keyword-only params (all default to today's exact behaviour) exist
    for the degraded-mode fill ladder in generate_recipes.py — each one
    relaxes a single dimension so a caller can widen the search step by step
    instead of giving up when both AI providers are down:
      - relax_suggested: drop the 7-day "recently shown" exclusion (see
        history.RECENTLY_SHOWN_WINDOW_DAYS) — a repeat is better than
        nothing once AI generation itself is unavailable.
      - min_similarity: override SIMILARITY_THRESHOLD.
      - any_cuisine: drop the hard preferred-cuisine filter.
      - include_stubs: a stub needs a live AI call to expand — poison for a
        providers-down fallback, so the ladder always passes False here.
    The 60-day "actually used" exclusion, the explicit-discard exclusion,
    avoid_titles, and allergen/dislike filters are NEVER relaxed, on any path.
    max_time_minutes joins that same never-relaxed list (2026-07-19, real bug
    found live: a 30-min pool recipe was offered for a <=5-min breakfast
    request) — the section header promises "<= X min" to the user's face, so
    the degraded-mode ladder below must never loosen it. Rows with an
    unparseable/missing `time` pass through un-excluded (unknown isn't
    grounds for exclusion, only a confirmed-over-limit time is).

    2026-07-16: avoid_titles closes a real gap found live — pool matching
    only ever excluded by exact recipe id (exclude_ids), so two DIFFERENT
    rows sharing the same title (e.g. two separately-created "Pollo al
    Ajillo" recipes from different past sessions) could both surface in the
    same multi-slot batch. Caller passes the batch's accumulated recent
    titles (case-insensitive); this is a hard filter, not the soft
    used_cuisines-style fallback below — showing the same dish twice in one
    request is worse than a thinner candidate pool.

    2026-07-16, same day, second correction: relax_suggested was removed
    entirely earlier today (replaced by the permanent, explicit discard
    signal), reasoning that "shown but not picked" should always be free to
    resurface. That fixed the diagnosed 163-stale-titles bug, but a live
    regression followed within hours: with zero short-term dedup left, the
    pool's deterministic similarity ranking (same profile+cuisine+meal_type
    -> same top candidate every time) and the model's own tendency to
    converge on the same "obvious" dish both kept resurfacing the exact same
    recipe on consecutive separate requests. Brought relax_suggested back,
    now backed by history.get_recently_suggested_recipe_ids's 7-day window
    instead of the old 28-day one — short session-scoped cooldown against
    IMMEDIATE repeats, not the old blanket multi-week ban. Coexists with,
    never replaces, the permanent discard exclusion.

    NOT implemented here (no data source exists yet, would be a fake check):
    "recipes whose required_ingredient_tags the user can mostly satisfy" needs
    a pantry data model that doesn't exist pre-onboarding — same gap as the
    documented-deferred country-availability filter. Skipped honestly rather
    than faked.

    2026-07-21, real bug found live: a pool recipe ("Italian Breakfast
    Cornetto with Almond Cream and Fresh Berries") never actually baked the
    dough it claimed to make — recipe_audit.py's semantic check had already
    flagged it (missing_defining_ingredient: "cornetto") days earlier, but
    that finding just sat unresolved in recipe_audit_findings, never
    consulted by serving — flag-and-report with nothing downstream reading
    the flags. Now a hard, never-relaxed exclusion (same tier as allergens/
    max_time_minutes): any recipe with an UNRESOLVED missing_defining_
    ingredient finding is never served from the pool until a human resolves
    it. Deliberately only this one check_name, not kcal findings too — kcal_
    suspiciously_round/implausible are much noisier (see the cross-provider
    kcal audit from 2026-07-20) and would exclude a large, mostly-fine slice
    of the pool; missing_defining_ingredient is the AI's own judgment that
    the dish itself is wrong, a much higher-confidence signal to act on.

    category_keywords (2026-07-20, fixed-breakfast-categories): when given,
    a candidate must have at least one of these keywords in its title or an
    ingredient name (case-insensitive substring) — a hard filter, same tier
    as avoid_titles, since a "yogurt" slot showing a dish with no yogurt in
    it defeats the whole point. Never relaxed by the degraded-mode ladder for
    the same reason avoid_titles/allergens aren't. A miss here just returns
    no pool match (same as any other None) — the caller falls back to fresh
    generation with the equivalent category instruction, not to this
    function trying harder.
    """
    query_text = build_query_text(profile, meal_type, any_cuisine=any_cuisine)
    query_vector_literal = await _get_query_vector_literal(query_text)
    if query_vector_literal is None:
        # Cloudflare unavailable and no cache hit -> no pool match, not a
        # crash. ADR 2's own fallback ("no qualifying match -> both fresh")
        # already covers exactly this case; the caller doesn't need to know why.
        return None

    recently_used = await history.get_recently_used_recipe_ids(profile.user_id)
    discarded = await history.get_discarded_recipe_ids(profile.user_id)
    excluded = recently_used | discarded | (exclude_ids or set())
    if not relax_suggested:
        excluded |= await history.get_recently_suggested_recipe_ids(profile.user_id)

    statuses = ["complete", "partial"] + (["stub"] if include_stubs else [])
    # Exclusion moved into SQL (not a Python post-filter) so LIMIT applies to
    # eligible rows: with a heavily-excluded pool (repeat testing, a thin
    # cuisine), the top-N by raw similarity can be 100% excluded ids while a
    # qualifying candidate sits just past the old cutoff.
    #
    # max_time_minutes joined this same SQL-level filter (2026-07-19, real
    # bug found live): it originally ran as a Python post-filter AFTER this
    # query's LIMIT — with a tight time budget, the top-20 nearest-by-
    # embedding rows could be 100% too-slow (embedding similarity has no
    # notion of cook time) while genuinely fast candidates sat unconsidered
    # past the cutoff, exactly the failure mode the comment above already
    # fixed once for exclusion. `time !~* 'hour'` rejects any hour-scale
    # claim outright (never fits a tight minute budget); the regexp mirrors
    # guardrails.parse_time_minutes's leading-integer extraction. A NULL or
    # unparseable `time` always passes — unknown is never grounds for
    # exclusion, only a confirmed-over-limit value is.
    rows = await db.pool().fetch(
        """
        SELECT id, title, brief_description, cuisine, main_protein, ingredients,
               image_url, status, source, base_serves, time, kcal, variations,
               embedding <=> $1::vector AS distance
        FROM recipes
        WHERE status = ANY($2::text[]) AND embedding IS NOT NULL
          AND NOT (id::text = ANY($3::text[]))
          AND NOT EXISTS (
            SELECT 1 FROM recipe_audit_findings f
            WHERE f.recipe_id = recipes.id AND f.check_name = 'missing_defining_ingredient' AND f.resolved = false
          )
          AND (
            $5::int IS NULL
            OR time IS NULL
            OR NULLIF(regexp_replace(time, '[^0-9].*$', ''), '') IS NULL
            OR (time !~* 'hour' AND NULLIF(regexp_replace(time, '[^0-9].*$', ''), '')::int <= $5::int)
          )
        ORDER BY embedding <=> $1::vector
        LIMIT $4
        """,
        query_vector_literal,
        statuses,
        list(excluded),
        CANDIDATE_POOL_SIZE,
        max_time_minutes,
    )

    threshold = min_similarity if min_similarity is not None else SIMILARITY_THRESHOLD
    preferred_cuisines = {c.lower() for c in profile.cuisines}
    used_cuisines_lower = {c.lower() for c in (used_cuisines or []) if c}
    avoid_titles_lower = {t.lower() for t in (avoid_titles or set()) if t}
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
        if (row["title"] or "").lower() in avoid_titles_lower:
            continue
        if category_keywords and not _matches_category(row, category_keywords):
            continue
        if max_time_minutes:
            row_minutes = parse_time_minutes(row.get("time"))
            if row_minutes is not None and row_minutes > max_time_minutes:
                continue
        if (1 - row["distance"]) < threshold:
            continue
        if row_cuisine in used_cuisines_lower:
            if fallback is None:
                fallback = dict(row)
            continue
        return dict(row)

    return fallback


async def find_oldest_repeat_candidates(
    profile: UserProfile,
    meal_type: str,
    exclude_ids: set[str],
    needed: int,
    max_time_minutes: int | None = None,
    avoid_titles: set[str] | None = None,
) -> list[dict]:
    """Absolute last resort (2026-07-19, user-requested): when even the fully
    -relaxed _pool_fill ladder in generate_recipes.py finds nothing, a
    genuine repeat beats an empty slot — but which repeat matters. This
    intentionally drops BOTH the 60-day "actually used" exclusion and the
    7-day "recently suggested" cooldown (the only two callers ever allowed
    to relax either), and orders by LEAST recently suggested instead of by
    embedding similarity — a recipe never suggested before (or suggested
    longest ago) is picked over one shown yesterday. Only the truly
    permanent signals stay hard: explicit discards, this batch's own
    exclude_ids/avoid_titles, allergies, and (still) max_time_minutes — a
    repeat is acceptable, an allergen or a dishonest time budget is not."""
    discarded = await history.get_discarded_recipe_ids(profile.user_id)
    excluded = discarded | (exclude_ids or set())
    avoid_titles_lower = {t.lower() for t in (avoid_titles or set()) if t}
    preferred_cuisines = {c.lower() for c in profile.cuisines}

    rows = await db.pool().fetch(
        """
        SELECT r.id, r.title, r.brief_description, r.cuisine, r.main_protein, r.ingredients,
               r.image_url, r.status, r.source, r.base_serves, r.time, r.kcal, r.variations,
               MAX(s.suggested_at) AS last_suggested_at
        FROM recipes r
        LEFT JOIN recipe_suggestions s ON s.recipe_id = r.id AND s.user_id = $1
        WHERE r.status IN ('complete', 'partial')
          AND NOT (r.id::text = ANY($2::text[]))
          AND (
            $3::int IS NULL
            OR r.time IS NULL
            OR NULLIF(regexp_replace(r.time, '[^0-9].*$', ''), '') IS NULL
            OR (r.time !~* 'hour' AND NULLIF(regexp_replace(r.time, '[^0-9].*$', ''), '')::int <= $3::int)
          )
        GROUP BY r.id
        ORDER BY last_suggested_at ASC NULLS FIRST
        LIMIT 50
        """,
        profile.user_id,
        list(excluded),
        max_time_minutes,
    )

    picked: list[dict] = []
    for row in rows:
        if len(picked) >= needed:
            break
        row_cuisine = (row["cuisine"] or "").lower()
        if preferred_cuisines and row_cuisine not in preferred_cuisines:
            continue
        if row["main_protein"] and row["main_protein"] in profile.dislikes:
            continue
        if _has_allergen_conflict(row["ingredients"], profile.allergies):
            continue
        if (row["title"] or "").lower() in avoid_titles_lower:
            continue
        picked.append(dict(row))

    # Cuisine is a soft preference everywhere else in this module — honour
    # that here too rather than leaving a slot empty over it, once the
    # preferred-cuisine pass above didn't fill every needed spot.
    if len(picked) < needed:
        picked_ids = {p["id"] for p in picked}
        for row in rows:
            if len(picked) >= needed:
                break
            if row["id"] in picked_ids:
                continue
            if row["main_protein"] and row["main_protein"] in profile.dislikes:
                continue
            if _has_allergen_conflict(row["ingredients"], profile.allergies):
                continue
            if (row["title"] or "").lower() in avoid_titles_lower:
                continue
            picked.append(dict(row))

    return picked


def _matches_category(row: dict, category_keywords: list[str]) -> bool:
    haystack = (row.get("title") or "") + " " + " ".join(
        ing.get("name", "") for ing in (row.get("ingredients") or [])
    )
    haystack = haystack.lower()
    return any(kw in haystack for kw in category_keywords)


def _has_allergen_conflict(ingredients: list[dict] | None, allergies: list[str]) -> bool:
    if not ingredients or not allergies:
        return False
    for ingredient in ingredients:
        if set(ingredient.get("allergen_tags", [])) & set(allergies):
            return True
    return False
