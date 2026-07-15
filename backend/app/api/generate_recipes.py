import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException

from app.core import db
from app.core.security import get_current_user_id
from app.models.generate import (
    GenerateRecipesAccepted,
    GenerateRecipesRequest,
    GenerateRecipesResponse,
    GenerationJobStatus,
    SlotOptions,
    SlotRequest,
)
from app.models.recipes import RecipeOption
from app.services import cloud_tasks, fresh_generation, guardrails, history, image_chain, pool_search, profile
from app.services.ai_client import AIProviderExhausted
from app.services.rate_limit import check_and_record_generation
from app.services.stub_expansion import expand_stub

router = APIRouter(prefix="/api", tags=["recipes"])

# Slot-complete callback signature: (slot_index, day, meal_type, options) -> None.
# Used by the async worker (app/api/internal.py) to persist each slot's
# result incrementally as it finishes, so a retried job only redoes whatever
# didn't complete last time instead of the whole batch from scratch.
OnSlotComplete = Callable[[int, str, str, list[dict]], Awaitable[None]]


class JobSlotsIncomplete(Exception):
    """Raised after a generation pass when one or more slots ended with zero
    options (both AI providers down AND the pool-fill ladder found nothing
    safe) or hit an unexpected error. Deliberately propagates to the worker
    as a real failure — even with Cloud Tasks retrying, a slot that silently
    stays empty forever is worse than one that keeps getting retried until
    the pool/providers recover (see internal.py's age-cutoff for the
    eventual give-up point)."""

    def __init__(self, failures: list[tuple[int, str]]):
        self.failures = failures
        super().__init__(f"{len(failures)} slot(s) incomplete: {failures}")


@router.post("/generate-recipes", response_model=GenerateRecipesAccepted, status_code=202)
async def generate_recipes(
    request: GenerateRecipesRequest, user_id: str = Depends(get_current_user_id)
):
    """Async-from-the-start (2026-07-14): the actual generation work always
    runs in the Cloud Tasks worker (app/api/internal.py's
    generate-recipes-batch handler), never inline in this request — a Cloud
    Run container can freeze its CPU once a response is sent, so there's no
    reliable way to "keep working after responding" other than a fresh,
    separately-dispatched request. The frontend polls GET
    /api/generate-recipes/{job_id}; for the common case (small batch) this
    still feels synchronous since the worker usually finishes in a few
    seconds, but nothing here is time-limited by this request's own
    lifetime."""
    monday = guardrails.normalise_to_monday(request.week_start)
    await check_and_record_generation(user_id, guardrails.week_id_for(monday))

    row = await db.pool().fetchrow(
        "INSERT INTO generation_jobs (user_id, week_start, slots_request) "
        "VALUES ($1, $2, $3::jsonb) RETURNING id",
        user_id,
        monday,
        [s.model_dump(mode="json") for s in request.slots],
    )
    job_id = str(row["id"])
    # Deliberately NOT swallowed (unlike select_recipe.py's steps-generation
    # enqueue, which can safely no-op since the meal is already committed
    # regardless) — nothing has happened yet here, so an enqueue failure
    # should surface as a normal error through the same apiFetch error path
    # the frontend already has for everything else.
    await cloud_tasks.enqueue_generate_recipes_batch(job_id)

    return GenerateRecipesAccepted(job_id=job_id)


# Lazy-finalizer horizon: belt-and-braces backstop for the (rare) case the
# Cloud Tasks queue itself gives up or a task gets purged before the
# worker's own 20h age-cutoff (internal.py) ever runs — without this, a job
# stuck in 'pending'/'running' with no further attempts would poll forever
# with no terminal state. No email here (that's the worker's job, on the
# common path); this only fires for a user actively looking at a truly
# ancient job.
JOB_LAZY_FINALIZE_AGE_HOURS = 24


@router.get("/generate-recipes/{job_id}", response_model=GenerationJobStatus)
async def get_generation_job(job_id: str, user_id: str = Depends(get_current_user_id)):
    """Exposes partial results while status is 'running' or 'failed' —
    internal.py already persists each slot as it completes, and a job can
    fail with real partial progress still worth showing."""
    row = await db.pool().fetchrow(
        "SELECT * FROM generation_jobs WHERE id = $1 AND user_id = $2", job_id, user_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    job = dict(row)
    if job["status"] in ("pending", "running"):
        age_hours = (datetime.now(UTC) - job["created_at"]).total_seconds() / 3600
        if age_hours > JOB_LAZY_FINALIZE_AGE_HOURS:
            claimed = await db.pool().fetchrow(
                "UPDATE generation_jobs SET status = 'failed', "
                "error = coalesce(error, 'Timed out waiting for available capacity.'), updated_at = now() "
                "WHERE id = $1 AND status NOT IN ('complete', 'failed') RETURNING *",
                job_id,
            )
            if claimed:
                job = dict(claimed)

    result = None
    if job["status"] in ("running", "complete", "failed") and job.get("result"):
        result = _job_result_to_response(job)

    return GenerationJobStatus(job_id=job_id, status=job["status"], result=result, error=job.get("error"))


def _job_result_to_response(job: dict) -> GenerateRecipesResponse:
    """job['result'] is stored as {"<slot_idx>": {day, meal_type, options}} —
    reassemble it into the real response shape, in original slot order.
    Tolerant of partial results (job still 'running', or 'failed' with
    whatever finished before the cutoff): only includes slots that actually
    finished with real options, skipping ones not in `result` yet or that
    were persisted empty, rather than assuming every index is present."""
    monday = guardrails.normalise_to_monday(job["week_start"])
    week_id = guardrails.week_id_for(monday)
    slots_request = job["slots_request"]
    result = job["result"] or {}

    slots = [
        SlotOptions(
            day=result[str(idx)]["day"],
            meal_type=result[str(idx)]["meal_type"],
            options=[RecipeOption(**o) for o in result[str(idx)]["options"]],
        )
        for idx in range(len(slots_request))
        if str(idx) in result and result[str(idx)]["options"]
    ]
    return GenerateRecipesResponse(
        week_id=week_id,
        week_start=monday,
        slots=slots,
        total_slots=len(slots_request),
        completed_slots=len(slots),
    )


async def _run_generation(
    user_id: str,
    week_start: date,
    slots: list[SlotRequest],
    already_complete: dict[int, list[dict]] | None = None,
    on_slot_complete: OnSlotComplete | None = None,
) -> GenerateRecipesResponse:
    """The actual generation pipeline — same logic as before this file's
    async refactor, just extracted out of the route handler so both the
    (now-thin) POST route's synchronous callers and the async worker can
    reuse it identically. already_complete/on_slot_complete exist purely for
    the worker's incremental-persistence/resume-on-retry behaviour; a plain
    call with neither set behaves exactly as the old inline version did."""
    monday = guardrails.normalise_to_monday(week_start)
    week_id = guardrails.week_id_for(monday)
    already_complete = already_complete or {}

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

    # Seed cross-slot dedup state from whatever a PRIOR attempt already
    # finished (worker retry case) — so a resumed job still avoids repeating
    # those dishes/cuisines in the slots it still has left.
    for options in already_complete.values():
        used_recipe_ids.update(str(o["id"]) for o in options)
        batch_history.extend(
            {"title": o["title"], "main_protein": o.get("main_protein")} for o in options
        )
        used_cuisines.extend(o["cuisine"] for o in options if o.get("cuisine"))

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
    groups: dict[str, list[tuple[int, SlotRequest]]] = {}
    for idx, slot in enumerate(slots):
        if idx not in already_complete:
            groups.setdefault(slot.meal_type, []).append((idx, slot))

    slot_options: dict[int, list[dict]] = dict(already_complete)
    # Appended to (never raised) from inside a group — a slot that comes back
    # empty or raises must not kill its sibling slots in the same group, and
    # groups run concurrently via gather below, so this is the single place
    # failures are collected before deciding what to do about them. Plain
    # list append is safe with no locking: asyncio's single-threaded event
    # loop means only one coroutine ever runs between awaits, same reasoning
    # already relied on for the shared used_recipe_ids/used_cuisines below.
    failed_slots: list[tuple[int, str]] = []

    async def _run_group(slots_with_idx):
        for idx, slot in slots_with_idx:
            comment = guardrails.sanitize_user_comment(slot.comment)
            try:
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
            except Exception as exc:
                failed_slots.append((idx, repr(exc)))
                continue
            if not options:
                failed_slots.append((idx, "no options (providers down and pool-fill found nothing safe)"))
                continue
            used_recipe_ids.update(str(o["id"]) for o in options)
            batch_history.extend(
                {"title": o["title"], "main_protein": o.get("main_protein")} for o in options
            )
            used_cuisines.extend(o["cuisine"] for o in options if o.get("cuisine"))
            slot_options[idx] = options
            if on_slot_complete:
                await on_slot_complete(idx, slot.day, slot.meal_type, options)

    await asyncio.gather(*(_run_group(group) for group in groups.values()))

    if failed_slots:
        raise JobSlotsIncomplete(failed_slots)

    slots_out = [
        SlotOptions(
            day=slot.day,
            meal_type=slot.meal_type,
            options=[RecipeOption(**_to_option_dict(o)) for o in slot_options[idx]],
        )
        for idx, slot in enumerate(slots)
    ]

    return GenerateRecipesResponse(week_id=week_id, week_start=monday, slots=slots_out)


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
    fails / cost kill-switch hit -> two pool recipes.

    2026-07-14 addition (deliberate trade-off, not a bug fix): the pool is
    checked for a SECOND qualifying match before ever touching fresh
    generation. If the pool alone can satisfy the slot, fresh generation is
    skipped entirely — one fewer live AI call, and no image-generation call
    either, since both pool matches already have images. Cost: the old rule
    guaranteed one freshly-written option per slot (real novelty every time);
    this trades that guarantee for cheaper serving once the pool is well
    stocked (see pool_warmer.py's proportional-generation work) — accepted
    knowingly, not an oversight. The proactive second search itself is free
    either way (a Cloudflare embedding call + a DB query, no Groq/OpenRouter
    spend), so computing it "just in case" costs nothing even on the slots
    where it ends up unused.
    """
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

    # Proactive second pool search — computed before deciding whether fresh
    # generation is needed at all, not just as the post-failure fallback the
    # `pool_option and generation_failed` branch below already had. Kept
    # around (not re-queried later) so that existing fallback branch can
    # reuse it directly instead of duplicating the search.
    second_pool_option = None
    if pool_option:
        second_pool_option = await pool_search.search_recipe_pool(
            user_profile,
            meal_type,
            exclude_ids | {str(pool_option["id"])},
            used_cuisines=used_cuisines + ([pool_option["cuisine"]] if pool_option.get("cuisine") else []),
        )
        if second_pool_option and second_pool_option["status"] == "stub":
            try:
                second_pool_option = await expand_stub(second_pool_option)
            except AIProviderExhausted:
                second_pool_option = None

    if pool_option and second_pool_option:
        options = [pool_option, second_pool_option]
        await _log_suggestions(user_id, options)
        return options

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
        # second_pool_option was already searched for proactively above
        # (before fresh generation was even attempted) — reaching this
        # branch means it came back empty (if it hadn't, the early-exit
        # above would have already returned), so there's nothing new a
        # re-query would find. Reusing it here just skips a guaranteed-
        # identical repeat search instead of re-running one.
        options = [o for o in (pool_option, second_pool_option) if o]
    else:
        options = []

    # Degraded-mode fallback: whatever combination of pool/fresh attempts
    # above landed on fewer than 2 options (both providers down with no pool
    # match, one fresh call exhausted mid-way, etc.) — top up from the pool
    # via progressively relaxed constraints rather than leaving the slot
    # short or empty. A slot ends with 0 options only if the fully-relaxed
    # pool has nothing allergen/dislike-safe left either.
    if len(options) < 2:
        options += await _pool_fill(
            user_profile,
            meal_type,
            exclude_ids | {str(o["id"]) for o in options},
            used_cuisines,
            2 - len(options),
        )

    # Image-text sync rule: ONLY freshly-generated options ever get a
    # synchronous, in-request image call — pool matches and expanded stubs
    # already have an image (or are queued and shown with a placeholder) and
    # must never wait. When both options in a slot are fresh, this fires both
    # image calls in parallel to halve the wait, not one after another.
    if fresh_options:
        await _generate_and_attach_images(fresh_options, user_id)

    await _log_suggestions(user_id, options)
    return options


_POOL_FILL_RUNGS: list[dict] = [
    {},
    {"relax_suggested": True},
    {"relax_suggested": True, "min_similarity": 0.55},
    {"relax_suggested": True, "min_similarity": 0.55, "any_cuisine": True},
]


async def _pool_fill(
    user_profile: profile.UserProfile,
    meal_type: str,
    exclude_ids: set[str],
    used_cuisines: list[str],
    needed: int,
) -> list[dict]:
    """Degraded-mode fallback when AI generation couldn't fill a slot (both
    providers exhausted/erroring): walk the pool with progressively relaxed
    constraints instead of leaving the slot short or empty. Allergen/dislike
    filters and the 60-day "actually used" exclusion are NEVER relaxed —
    only the 28-day "recently shown" window, the similarity threshold, and
    the preferred-cuisine filter loosen, one rung at a time, only as far as
    needed. include_stubs=False on every rung: a stub needs a live AI call
    to expand, which is exactly what's unavailable here.
    """
    filled: list[dict] = []
    excluded = set(exclude_ids)
    cuisines = list(used_cuisines)
    for rung in _POOL_FILL_RUNGS:
        if len(filled) >= needed:
            break
        while len(filled) < needed:
            candidate = await pool_search.search_recipe_pool(
                user_profile,
                meal_type,
                excluded,
                used_cuisines=cuisines,
                include_stubs=False,
                **rung,
            )
            if not candidate:
                break
            filled.append(candidate)
            excluded.add(str(candidate["id"]))
            if candidate.get("cuisine"):
                cuisines.append(candidate["cuisine"])
    return filled


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
