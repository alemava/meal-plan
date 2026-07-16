from datetime import date, timedelta

from app.core import db

REPETITION_WINDOW_DAYS = 60  # hard-filter exclusion for pool matches — tunable, start conservative
RECENT_CONTEXT_DAYS = 14  # injected into the fresh-generation system prompt

# 2026-07-16, second correction the same day: removing the old 28-day
# "anything shown gets excluded" behaviour (see get_discarded_* below) fixed
# the 163-stale-titles bug, but left ZERO short-term deduplication — a real
# regression found live within hours: the exact same fresh-generated dish
# ("Vegetable Pad Thai") appeared twice in 34 seconds across two separate
# requests, and pool matches are a fully deterministic similarity search, so
# the same top-ranked candidate kept winning on every request too. Neither
# had been selected or discarded, so nothing signalled "you just showed
# this." This is NOT a revival of the old 28-day exclusion — 7 days is a
# short session-scoped cooldown against IMMEDIATE repeats, not a blanket
# multi-week ban, and it coexists with (never replaces) the permanent,
# explicit get_discarded_* signal below. User's explicit choice of window.
RECENTLY_SHOWN_WINDOW_DAYS = 7


async def get_user_history(user_id: str, days: int = RECENT_CONTEXT_DAYS) -> list[dict]:
    """Recent recipe titles + main proteins — used both as a model-callable tool
    (get_user_history) and injected directly into the system prompt for fresh
    generation (step 18b), since the model has no memory of its own."""
    since = date.today() - timedelta(days=days)
    rows = await db.pool().fetch(
        """
        SELECT r.title, r.main_protein, h.used_on
        FROM user_recipe_history h
        JOIN recipes r ON r.id = h.recipe_id
        WHERE h.user_id = $1 AND h.used_on >= $2
        ORDER BY h.used_on DESC
        """,
        user_id,
        since,
    )
    return [
        {"title": row["title"], "main_protein": row["main_protein"], "used_on": str(row["used_on"])}
        for row in rows
    ]


async def get_recently_used_recipe_ids(
    user_id: str, days: int = REPETITION_WINDOW_DAYS
) -> set[str]:
    """Hard-filter exclusion for pool matches (step 15) — a separate, longer
    window than the 14-day context used for fresh-generation prompting above."""
    since = date.today() - timedelta(days=days)
    rows = await db.pool().fetch(
        "SELECT DISTINCT recipe_id FROM user_recipe_history WHERE user_id = $1 AND used_on >= $2",
        user_id,
        since,
    )
    return {str(row["recipe_id"]) for row in rows}


async def get_discarded_recipe_ids(user_id: str) -> set[str]:
    """2026-07-16 replacement for the old get_recently_suggested_recipe_ids:
    that one excluded anything merely SHOWN for 28 days, whether the user
    picked it or not — diagnosed live as the real cause of a heavily-tested
    account accumulating 163 "avoid repeating" titles and a ~46% fresh-
    generation retry rate, since a shown-but-not-picked recipe is still
    perfectly fine to resurface. This is the explicit signal instead: only a
    recipe the user actively told us they don't want again, permanently (no
    time window) — never relaxed, same category as the 60-day used-recipe
    exclusion above. Per-user, not global — a recipe one user discards can
    still be shown to a different user."""
    rows = await db.pool().fetch(
        "SELECT recipe_id FROM recipe_discards WHERE user_id = $1", user_id
    )
    return {str(row["recipe_id"]) for row in rows}


async def get_discarded_titles(user_id: str) -> list[dict]:
    """The other half of get_discarded_recipe_ids above: that one only feeds
    pool-search's id-exclusion — fresh generation's "avoid repeating"
    prompt/guardrail needs titles, same shape as get_user_history so callers
    can just concatenate the two lists. Ordered newest-first so a future
    LIMIT is a one-line change if a heavy user's discard list ever grows
    large enough to matter (same class of problem this replaces, just far
    slower to recur since it needs a deliberate click, not a byproduct of
    every generation)."""
    rows = await db.pool().fetch(
        """
        SELECT r.title, r.main_protein
        FROM recipe_discards d
        JOIN recipes r ON r.id = d.recipe_id
        WHERE d.user_id = $1
        ORDER BY d.discarded_at DESC
        """,
        user_id,
    )
    return [{"title": row["title"], "main_protein": row["main_protein"]} for row in rows]


async def get_recently_suggested_recipe_ids(
    user_id: str, days: int = RECENTLY_SHOWN_WINDOW_DAYS
) -> set[str]:
    """Short cooldown against showing the SAME recipe again immediately —
    see RECENTLY_SHOWN_WINDOW_DAYS above for why this exists alongside (not
    instead of) get_discarded_recipe_ids. Relaxable in degraded mode (a
    repeat is better than nothing when both AI providers are down) — unlike
    the discard exclusion, which never relaxes."""
    since = date.today() - timedelta(days=days)
    rows = await db.pool().fetch(
        "SELECT DISTINCT recipe_id FROM recipe_suggestions WHERE user_id = $1 AND suggested_at >= $2",
        user_id,
        since,
    )
    return {str(row["recipe_id"]) for row in rows}


async def get_recently_suggested_titles(
    user_id: str, days: int = RECENTLY_SHOWN_WINDOW_DAYS
) -> list[dict]:
    """The other half of get_recently_suggested_recipe_ids above: that one
    only feeds pool-search's id-exclusion — fresh generation's "avoid
    repeating" prompt needs titles, same shape as get_user_history so
    callers can just concatenate the lists. This is specifically what closes
    the fresh-generation half of the 2026-07-16 regression (a model
    independently regenerating the identical dish name minutes apart,
    unprompted, since nothing told it "you just suggested this")."""
    since = date.today() - timedelta(days=days)
    rows = await db.pool().fetch(
        """
        SELECT DISTINCT r.title, r.main_protein
        FROM recipe_suggestions s
        JOIN recipes r ON r.id = s.recipe_id
        WHERE s.user_id = $1 AND s.suggested_at >= $2
        """,
        user_id,
        since,
    )
    return [{"title": row["title"], "main_protein": row["main_protein"]} for row in rows]
