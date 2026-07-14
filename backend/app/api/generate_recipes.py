import asyncio

from fastapi import APIRouter, Depends

from app.core import db
from app.core.security import get_current_user_id
from app.models.generate import GenerateRecipesRequest, GenerateRecipesResponse, SlotOptions
from app.models.recipes import RecipeOption
from app.services import fresh_generation, guardrails, history, image_chain, pool_search, profile
from app.services.ai_client import AIProviderExhausted
from app.services.rate_limit import check_and_record_generation
from app.services.stub_expansion import expand_stub

router = APIRouter(prefix="/api", tags=["recipes"])


@router.post("/generate-recipes", response_model=GenerateRecipesResponse)
async def generate_recipes(
    request: GenerateRecipesRequest, user_id: str = Depends(get_current_user_id)
):
    monday = guardrails.normalise_to_monday(request.week_start)
    week_id = guardrails.week_id_for(monday)

    await check_and_record_generation(user_id, week_id)

    user_profile = await profile.load_profile(user_id)
    # Grows with every slot's picks below — a real gap found live: requesting
    # several slots of the SAME meal_type in one batch (e.g. "4 lunches")
    # generated each one fully independently, with no idea what the others
    # had already produced, so they'd converge on the same popular dish
    # (three mushroom risottos in one batch, seen live). Seeding it with real
    # history and extending it after each slot reuses the exact same
    # "avoid repeating" prompt mechanism for batch-level variety too.
    #
    # Seeded from BOTH get_user_history (selected meals) AND
    # get_recently_suggested_titles (shown but maybe never selected) — the
    # exact-title guardrail and "avoid repeating" instruction previously only
    # knew about selected meals, so a dish shown one day but never picked
    # could be regenerated verbatim the next day with nothing catching it
    # (seen live: the identical title, freshly generated 24h apart).
    batch_history = list(await history.get_user_history(user_id))
    batch_history.extend(await history.get_recently_suggested_titles(user_id))

    used_recipe_ids: set[str] = set()
    # Same rationale as batch_history above, applied to cuisine instead of
    # title/protein: requesting several slots in one batch with no shared
    # signal let them all converge on the same popular cuisine (e.g. 3
    # Spanish dishes in a row) even though the profile prefers several.
    used_cuisines: list[str] = []

    # Real bug hit live: a 15-slot batch (5 breakfast + 5 lunch + 5 dinner)
    # processed one slot at a time took 300s+ and got killed by Cloud Run's
    # own request timeout before finishing. Slots of the SAME meal_type must
    # stay sequential — that's exactly where the cross-slot dedup above
    # (batch_history/used_recipe_ids/used_cuisines) earns its keep, since
    # that's where real duplicates were observed (three mushroom risottos in
    # one batch). Different meal types rarely compete for the same dish, so
    # running one group per meal_type concurrently cuts wall-clock roughly by
    # the number of distinct meal types requested (up to 3x for a full-week
    # breakfast+lunch+dinner batch) without touching that tuned dedup logic —
    # asyncio's single-threaded event loop means the shared lists/set below
    # are safely mutated across groups with no locking needed.
    groups: dict[str, list[tuple[int, object]]] = {}
    for idx, slot in enumerate(request.slots):
        groups.setdefault(slot.meal_type, []).append((idx, slot))

    slot_options: dict[int, list[dict]] = {}

    async def _run_group(slots_with_idx):
        for idx, slot in slots_with_idx:
            comment = guardrails.sanitize_user_comment(slot.comment)
            options = await _generate_options_for_slot(
                user_profile,
                slot.meal_type,
                batch_history,
                used_recipe_ids,
                user_id,
                comment,
                slot.max_time_minutes,
                used_cuisines,
            )
            used_recipe_ids.update(str(o["id"]) for o in options)
            batch_history.extend(
                {"title": o["title"], "main_protein": o.get("main_protein")} for o in options
            )
            used_cuisines.extend(o["cuisine"] for o in options if o.get("cuisine"))
            slot_options[idx] = options

    await asyncio.gather(*(_run_group(group) for group in groups.values()))

    slots = [
        SlotOptions(
            day=slot.day,
            meal_type=slot.meal_type,
            options=[RecipeOption(**_to_option_dict(o)) for o in slot_options[idx]],
        )
        for idx, slot in enumerate(request.slots)
    ]

    return GenerateRecipesResponse(week_id=week_id, week_start=monday, slots=slots)


async def _generate_options_for_slot(
    user_profile: profile.UserProfile,
    meal_type: str,
    recent_history: list[dict],
    exclude_ids: set[str],
    user_id: str,
    comment: str | None = None,
    max_time_minutes: int | None = None,
    used_cuisines: list[str] | None = None,
) -> list[dict]:
    """ADR 2 fallbacks: no qualifying pool match -> both fresh; generation
    fails / cost kill-switch hit -> two pool recipes."""
    # Local copy, extended as each option within THIS slot is picked, so the
    # slot's own second option also avoids repeating the first one's cuisine
    # — not just cuisines from earlier slots in the batch.
    used_cuisines = list(used_cuisines or [])

    pool_option = await pool_search.search_recipe_pool(
        user_profile, meal_type, exclude_ids, used_cuisines=used_cuisines
    )
    if pool_option and pool_option["status"] == "stub":
        try:
            pool_option = await expand_stub(pool_option)
        except AIProviderExhausted:
            # Treated the same as "no qualifying pool match" (ADR2) — the
            # branches below already handle pool_option=None correctly by
            # falling through to fresh generation.
            pool_option = None
    if pool_option and pool_option.get("cuisine"):
        used_cuisines.append(pool_option["cuisine"])

    # The pool match's title+protein (if any) is already known before asking
    # for the fresh option — pass it along so the model doesn't reinvent the
    # same dish for this cuisine/meal_type as a "different" second option.
    sibling = (
        {"title": pool_option["title"], "main_protein": pool_option.get("main_protein")}
        if pool_option
        else None
    )

    fresh_option = None
    generation_failed = False
    try:
        fresh_option = await fresh_generation.generate_fresh_option(
            user_profile,
            meal_type,
            recent_history,
            sibling=sibling,
            comment=comment,
            max_time_minutes=max_time_minutes,
            used_cuisines=used_cuisines,
        )
    except AIProviderExhausted:
        generation_failed = True
    if fresh_option and fresh_option.get("cuisine"):
        used_cuisines.append(fresh_option["cuisine"])

    fresh_options: list[dict] = []
    if pool_option and fresh_option:
        options = [pool_option, fresh_option]
        fresh_options = [fresh_option]
    elif fresh_option and not pool_option:
        second_fresh = None
        fresh_sibling = {
            "title": fresh_option["title"],
            "main_protein": fresh_option.get("main_protein"),
        }
        try:
            second_fresh = await fresh_generation.generate_fresh_option(
                user_profile,
                meal_type,
                recent_history,
                sibling=fresh_sibling,
                comment=comment,
                max_time_minutes=max_time_minutes,
                used_cuisines=used_cuisines,
            )
        except AIProviderExhausted:
            pass
        options = [o for o in (fresh_option, second_fresh) if o]
        fresh_options = options
    elif pool_option and generation_failed:
        # search_recipe_pool's exclusion check compares against str(row["id"]) —
        # pool_option["id"] is a raw UUID from asyncpg, so it must be
        # stringified here or the exclusion silently never matches (a real bug
        # seen live: the same pool recipe shown twice as both "options").
        second_pool = await pool_search.search_recipe_pool(
            user_profile,
            meal_type,
            exclude_ids | {str(pool_option["id"])},
            used_cuisines=used_cuisines,
        )
        if second_pool and second_pool["status"] == "stub":
            try:
                second_pool = await expand_stub(second_pool)
            except AIProviderExhausted:
                second_pool = None
        options = [o for o in (pool_option, second_pool) if o]
    else:
        options = []

    # Image-text sync rule: ONLY freshly-generated options ever get a
    # synchronous, in-request image call — pool matches and expanded stubs
    # already have an image (or are queued and shown with a placeholder) and
    # must never wait. When both options in a slot are fresh, this fires both
    # image calls in parallel to halve the wait, not one after another.
    if fresh_options:
        await _generate_and_attach_images(fresh_options, user_id)

    await _log_suggestions(user_id, options)
    return options


async def _log_suggestions(user_id: str, options: list[dict]) -> None:
    """Per-user, 4-week no-repeat (history.SUGGESTION_REPETITION_WINDOW_DAYS):
    logs every option actually SHOWN, whether selected or not — a thin pool
    otherwise keeps resurfacing the same recipe as an option indefinitely,
    since the existing recently-used exclusion only fires on actual
    selection. Applies to fresh options too, since they become pool
    candidates for future searches the moment they're persisted."""
    for option in options:
        await db.pool().execute(
            "INSERT INTO recipe_suggestions (user_id, recipe_id) VALUES ($1, $2)",
            user_id,
            str(option["id"]),
        )


async def _generate_and_attach_images(options: list[dict], user_id: str) -> None:
    specs = [(o["id"], o["image_prompt"]) for o in options]
    urls = await image_chain.generate_images_for_live_options(specs, user_id=user_id)
    for option, url in zip(options, urls, strict=False):
        option["image_url"] = url
        if url:
            await db.pool().execute(
                "UPDATE recipes SET image_url = $1 WHERE id = $2", url, option["id"]
            )


def _to_option_dict(option: dict) -> dict:
    return {
        "id": str(option["id"]),
        "title": option["title"],
        "brief_description": option["brief_description"],
        "cuisine": option.get("cuisine"),
        "main_protein": option.get("main_protein"),
        "ingredients": option["ingredients"],
        "image_url": option.get("image_url"),
        "status": option["status"],
        "source": option["source"],
        "base_serves": option.get("base_serves"),
        "time": option.get("time"),
        "kcal": option.get("kcal"),
    }
