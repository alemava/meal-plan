from datetime import date, timedelta

from app.core import db

REPETITION_WINDOW_DAYS = 60  # hard-filter exclusion for pool matches — tunable, start conservative
RECENT_CONTEXT_DAYS = 14  # injected into the fresh-generation system prompt
SUGGESTION_REPETITION_WINDOW_DAYS = 28  # don't re-show a recipe as an option, per user, for 4 weeks


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


async def get_recently_suggested_recipe_ids(
    user_id: str, days: int = SUGGESTION_REPETITION_WINDOW_DAYS
) -> set[str]:
    """Separate from get_recently_used_recipe_ids above: this covers recipes
    merely SHOWN as an option, whether the user picked them or not — a real
    gap found live (a thin pool meant the same pool recipe kept surfacing as
    an option repeatedly since it was never actually selected, so it never
    hit the used-recipe exclusion). Per-user, not global — a recipe shown to
    one user today can still be shown to a different user tomorrow."""
    since = date.today() - timedelta(days=days)
    rows = await db.pool().fetch(
        "SELECT DISTINCT recipe_id FROM recipe_suggestions WHERE user_id = $1 AND suggested_at >= $2",
        user_id,
        since,
    )
    return {str(row["recipe_id"]) for row in rows}


async def get_recently_suggested_titles(
    user_id: str, days: int = SUGGESTION_REPETITION_WINDOW_DAYS
) -> list[dict]:
    """The other half of the gap above: get_recently_suggested_recipe_ids
    only fed pool-search's id-exclusion — fresh generation's "avoid
    repeating" prompt/guardrail only ever looked at get_user_history
    (SELECTED meals), so a dish shown but never selected could be
    regenerated verbatim days later with nothing catching it (seen live:
    the exact same title, freshly generated a day apart). Same shape as
    get_user_history so callers can just concatenate the two lists."""
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
