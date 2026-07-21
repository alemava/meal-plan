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

# 2026-07-20, user-requested: the first 3 breakfast slots in a batch each
# pin to one of these fixed categories (see SlotRequest.breakfast_category /
# index.html's slot-building loop for how a slot gets assigned one). Keywords
# drive pool_search's hard ingredient/title filter; labels drive
# fresh_generation's prompt instruction.
BREAKFAST_CATEGORY_KEYWORDS = {
    "yogurt": ["yogurt", "yoghurt", "skyr", "kefir"],
    "toast": ["bread", "toast", "baguette", "sourdough", "brioche", "bagel"],
    "eggs": ["egg", "eggs"],
}
BREAKFAST_CATEGORY_LABELS = {"yogurt": "yogurt", "toast": "toast/bread", "eggs": "eggs"}


def _pantry_item_used(pantry_name: str, options: list[dict]) -> bool:
    """Substring match, same tolerance as pool_search._matches_category —
    a pantry entry named "mango" should count as used by an ingredient named
    "diced mango" or "ripe mango, sliced", not just an exact string match."""
    pantry_name = pantry_name.strip().lower()
    if not pantry_name:
        return False
    for option in options:
        for ing in option.get("ingredients") or []:
            ing_name = (ing.get("name") or "").strip().lower()
            if ing_name and (pantry_name in ing_name or ing_name in pantry_name):
                return True
    return False

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

    # Sanitized ONCE, here, before it's ever persisted — the worker
    # (internal.py) reads generation_jobs.pantry back as plain dicts (already
    # jsonb-decoded), not PantryItem objects, so this is the only point where
    # guardrails.sanitize_pantry_ingredients's PantryItem-shaped input is
    # actually available.
    pantry = guardrails.sanitize_pantry_ingredients(request.pantry)

    row = await db.pool().fetchrow(
        "INSERT INTO generation_jobs (user_id, week_start, slots_request, pantry) "
        "VALUES ($1, $2, $3::jsonb, $4::jsonb) RETURNING id",
        user_id,
        monday,
        [s.model_dump(mode="json") for s in request.slots],
        pantry,
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
    pantry: list[dict] | None = None,
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
    # Seeded from get_user_history (selected meals), get_discarded_titles
    # (explicitly told "don't show me this again"), AND get_recently_
    # suggested_titles (shown in the last 7 days — see history.
    # RECENTLY_SHOWN_WINDOW_DAYS). That last one used to be a 28-day window
    # covering ANYTHING shown, diagnosed live as the real cause of a heavily-
    # tested account accumulating 163 "avoid repeating" titles — removed for
    # that reason, then partially reinstated (at 7 days, not 28) hours later
    # after a live regression: with zero short-term dedup, a fresh
    # generation independently repeated the exact same dish twice within 34
    # seconds across two separate requests, since nothing told the model it
    # had just suggested that title. Discards stay permanent and separate.
    batch_history = list(await history.get_user_history(user_id))
    batch_history.extend(await history.get_discarded_titles(user_id))
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
    # Deferred image generation (2026-07-18) — a slot returns as soon as its
    # TEXT is ready; any fresh option tagged _needs_image gets its photo
    # generated here, off to the side, then re-persists the same slot once
    # done. Collected as tasks (not awaited inline) so slot N+1's text isn't
    # held up behind slot N's images either.
    image_tasks: list[asyncio.Task] = []

    async def _fill_images_and_repersist(idx: int, day: str, meal_type: str, options: list[dict]) -> None:
        pending = [o for o in options if o.pop("_needs_image", False)]
        if not pending:
            return
        await _generate_and_attach_images(pending, user_id)
        if on_slot_complete:
            await on_slot_complete(idx, day, meal_type, options)

    # 2026-07-19 — this "known, accepted edge case" (see the comment further
    # down, near the final image_tasks gather) turned out to recur several
    # times in one day once retries got frequent, not the rare event it was
    # assumed to be: a worker killed mid-request during the image phase left
    # the slot "complete" text-wise, and a retry used to skip it entirely via
    # already_complete, permanently stranding it with no image (2 real cases
    # hit live the same day: "Arroz con Pollo", "Grilled Chicken with Lemon
    # and Herb Marinade"). Every already-complete option still missing
    # image_url now gets one real retry here, before the normal per-slot
    # groups even start. The persisted job result (built by _to_option_dict)
    # never carries image_prompt, so it's fetched fresh from `recipes` first.
    already_complete_missing_images = [
        o for options in already_complete.values() for o in options if not o.get("image_url")
    ]
    if already_complete_missing_images:
        prompt_rows = await db.pool().fetch(
            "SELECT id, image_prompt FROM recipes WHERE id = ANY($1::uuid[])",
            [o["id"] for o in already_complete_missing_images],
        )
        prompts_by_id = {str(row["id"]): row["image_prompt"] for row in prompt_rows}
        for o in already_complete_missing_images:
            o["image_prompt"] = prompts_by_id.get(o["id"])
            o["_needs_image"] = True
        for idx, options in already_complete.items():
            if any(o.get("_needs_image") for o in options):
                slot = slots[idx]
                image_tasks.append(
                    asyncio.create_task(_fill_images_and_repersist(idx, slot.day, slot.meal_type, options))
                )

    async def _run_group(slots_with_idx):
        # 2026-07-21, user-requested: each pantry item should be used at most
        # ONCE per meal type across the whole batch — e.g. mango in one
        # breakfast and jamón in a different one, not both repeating mango.
        # A single meal combining several pantry items still only counts as
        # one use of each (see _pantry_item_used below). Groups run one per
        # meal_type (see the module docstring above), each starting from its
        # own full copy of `pantry` — that's what lets the SAME item recur
        # once in breakfast, once in lunch, once in dinner, while never
        # repeating within one meal type's own slots.
        available_pantry = list(pantry) if pantry else pantry
        for idx, slot in slots_with_idx:
            comment = guardrails.sanitize_user_comment(slot.comment)
            # A slot normally yields 2 options together, and one of them can
            # be an LLM call taking real seconds — real user feedback (2026-
            # 07-18): cards still arrived in pairs even after images stopped
            # blocking text, because both options were only ever persisted
            # once, at the very end. This persists each option AS SOON as
            # _generate_options_for_slot has it (pool matches are near-
            # instant DB lookups anyway, so those still land together — it's
            # the LLM-generated ones this actually helps). Growing the SAME
            # slot's persisted list is safe/idempotent: internal.py's
            # _persist overwrites that slot's whole entry each call, so a
            # partial-then-fuller list for the same index never corrupts.
            partial: list[dict] = []

            async def _emit(option: dict, idx=idx, slot=slot, partial=partial) -> None:
                partial.append(option)
                if on_slot_complete:
                    await on_slot_complete(idx, slot.day, slot.meal_type, list(partial))

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
                    available_pantry,
                    on_option_ready=_emit,
                    options_needed=1 if slot.meal_type == "breakfast" else 2,
                    breakfast_category=slot.breakfast_category,
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
            if available_pantry:
                available_pantry = [p for p in available_pantry if not _pantry_item_used(p["name"], options)]
            slot_options[idx] = options
            if on_slot_complete:
                await on_slot_complete(idx, slot.day, slot.meal_type, options)
            if any(o.get("_needs_image") for o in options):
                image_tasks.append(
                    asyncio.create_task(_fill_images_and_repersist(idx, slot.day, slot.meal_type, options))
                )

    await asyncio.gather(*(_run_group(group) for group in groups.values()))
    # Must finish (success or failure — an image failure is never a job
    # failure, the existing queue+placeholder path already covers it) before
    # this function returns: the Cloud Tasks worker's request ends right
    # after, and Cloud Run can freeze a container's CPU once its response is
    # sent, which would silently strand any image task still in flight.
    #
    # If BATCH_WORKER_TIMEOUT_SECONDS is hit while an image task is still
    # running, asyncio.wait_for in internal.py cancels this whole function —
    # that slot's TEXT is already persisted as "complete" by then. This used
    # to be a dead end (a retry's already_complete skipped the slot entirely,
    # stranding it with no image forever) — no longer: see the already-
    # complete-missing-images backfill near the top of this function, which
    # gives exactly this case one real retry on the next attempt.
    if image_tasks:
        await asyncio.gather(*image_tasks, return_exceptions=True)

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
    pantry: list[dict] | None = None,
    on_option_ready: Callable[[dict], Awaitable[None]] | None = None,
    options_needed: int = 2,
    breakfast_category: str | None = None,
) -> list[dict]:
    """ADR 2 fallbacks: no qualifying pool match -> both fresh; generation
    fails / cost kill-switch hit -> two pool recipes.

    options_needed (2026-07-18): breakfast asks for 1, not 2 — real user
    feedback that breakfast tolerates far more repetition than lunch/dinner
    (confirmed against real meal-planning apps' own weekly/themed breakfast
    rotations), so generating 2 alternatives per requested breakfast slot
    was wasted variety nobody asked for. Handled as a genuinely separate,
    simpler path below (not a parametrized version of the 2-option branches)
    to keep the existing, more complex 2-option logic completely untouched.

    on_option_ready (2026-07-18), if given, fires once per option AS SOON as
    it's individually ready — not batched at the end — so the caller can
    persist/show it immediately rather than waiting for this slot's second
    option too. Pool matches are near-instant (DB lookup), so those still
    land close together in practice; this is what actually shortens the
    perceived wait for the LLM-generated ones.

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
    # 2026-07-16: pool matching only ever excluded by exact recipe id, so two
    # DIFFERENT rows sharing a title (e.g. two separately-created "Pollo al
    # Ajillo" recipes from past sessions) could both surface in the same
    # multi-slot batch — a real bug seen live. avoid_titles closes it; same
    # title source (recent_history) fresh generation already uses for its
    # own "avoid repeating" prompt/guardrail.
    avoid_titles = {h["title"] for h in recent_history if h.get("title")}

    # 2026-07-20 — only the single-option breakfast path (options_needed==1,
    # below) ever gets a breakfast_category; the 2-option lunch/dinner
    # branches never pass one, so this is a no-op there.
    category_keywords = BREAKFAST_CATEGORY_KEYWORDS.get(breakfast_category) if breakfast_category else None
    category_label = BREAKFAST_CATEGORY_LABELS.get(breakfast_category) if breakfast_category else None

    pool_option = await pool_search.search_recipe_pool(
        user_profile,
        meal_type,
        exclude_ids,
        used_cuisines=used_cuisines,
        avoid_titles=avoid_titles,
        max_time_minutes=max_time_minutes,
        category_keywords=category_keywords,
    )
    if pool_option and pool_option["status"] == "stub":
        try:
            pool_option = await expand_stub(pool_option)
        except AIProviderExhausted:
            # Treated the same as "no qualifying pool match" (ADR2) — the
            # branches below already handle pool_option=None correctly by
            # falling through to fresh generation.
            pool_option = None
    if pool_option and on_option_ready:
        await on_option_ready(pool_option)

    if options_needed == 1:
        if pool_option:
            await _log_suggestions(user_id, [pool_option])
            return [pool_option]
        fresh_option = None
        try:
            fresh_option = await fresh_generation.generate_fresh_option(
                user_profile,
                meal_type,
                recent_history,
                comment=comment,
                max_time_minutes=max_time_minutes,
                used_cuisines=used_cuisines,
                pantry=pantry,
                category_hint=category_label,
            )
            if fresh_option and on_option_ready:
                await on_option_ready(fresh_option)
        except AIProviderExhausted:
            pass
        options = [fresh_option] if fresh_option else []
        if not options:
            options = await _pool_fill(
                user_profile,
                meal_type,
                exclude_ids,
                used_cuisines,
                1,
                avoid_titles=avoid_titles,
                max_time_minutes=max_time_minutes,
            )
            if on_option_ready:
                for o in options:
                    await on_option_ready(o)
        elif fresh_option:
            fresh_option["_needs_image"] = True
        await _log_suggestions(user_id, options)
        return options

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
            avoid_titles=avoid_titles | {pool_option["title"]},
            max_time_minutes=max_time_minutes,
        )
        if second_pool_option and second_pool_option["status"] == "stub":
            try:
                second_pool_option = await expand_stub(second_pool_option)
            except AIProviderExhausted:
                second_pool_option = None

    if pool_option and second_pool_option:
        options = [pool_option, second_pool_option]
        if on_option_ready:
            await on_option_ready(second_pool_option)
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
            pantry=pantry,
        )
        if fresh_option and on_option_ready:
            await on_option_ready(fresh_option)
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
                pantry=pantry,
            )
            if second_fresh and on_option_ready:
                await on_option_ready(second_fresh)
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
        filled = await _pool_fill(
            user_profile,
            meal_type,
            exclude_ids | {str(o["id"]) for o in options},
            used_cuisines,
            2 - len(options),
            avoid_titles=avoid_titles | {o["title"] for o in options},
            max_time_minutes=max_time_minutes,
        )
        options += filled
        if on_option_ready:
            for o in filled:
                await on_option_ready(o)

    # Image-text sync rule RELAXED (2026-07-18, user feedback: cards arrived
    # in bursts because a slot's TEXT — ready in ~5s — sat blocked behind its
    # own image generation before either was shown). Only freshly-generated
    # options ever need an image at all (pool matches/expanded stubs already
    # have one, or are queued with a placeholder) — but that image no longer
    # blocks this slot's return. It's tagged here and generated afterward, in
    # the background, by _run_generation/_run_group below, which persists
    # the slot a second time once the image lands. The frontend already
    # renders the gradient placeholder for a null image_url and swaps in the
    # real photo whenever the next poll delivers it — no frontend change
    # needed for this half.
    for o in fresh_options:
        o["_needs_image"] = True

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
    avoid_titles: set[str] | None = None,
    max_time_minutes: int | None = None,
) -> list[dict]:
    """Degraded-mode fallback when AI generation couldn't fill a slot (both
    providers exhausted/erroring): walk the pool with progressively relaxed
    constraints instead of leaving the slot short or empty. Allergen/dislike
    filters, the 60-day "actually used" exclusion, the explicit-discard
    exclusion, avoid_titles, and max_time_minutes are NEVER relaxed — only
    the 7-day "recently shown" window, the similarity threshold, and the
    preferred-cuisine filter loosen, one rung at a time, only as far as
    needed. include_stubs=False on every rung: a stub needs a live AI call to
    expand, which is exactly what's unavailable here.

    Absolute last resort (2026-07-19, user-requested): if every rung above
    still leaves the slot short, fall through to
    pool_search.find_oldest_repeat_candidates — a genuine repeat, but the
    LEAST recently suggested one (or never-suggested), never a random one.
    This is the only path that relaxes the 60-day "actually used" exclusion;
    discards/allergies/max_time_minutes stay hard even here.

    2026-07-19, same-day fix: the inbound `avoid_titles` carries the 7-day
    "recently suggested" title list (from batch_history upstream) — exactly
    what this last resort exists to override. Passing it straight through
    silently defeated the whole point (a heavily-tested account can have
    24 of 26 real candidates already on that list, so almost nothing ever
    reached this fallback). The last-resort call now only avoids titles
    actually picked WITHIN this same _pool_fill invocation, tracked
    separately below.
    """
    filled: list[dict] = []
    excluded = set(exclude_ids)
    cuisines = list(used_cuisines)
    titles = set(avoid_titles or set())
    batch_only_titles: set[str] = set()
    for rung in _POOL_FILL_RUNGS:
        if len(filled) >= needed:
            break
        while len(filled) < needed:
            candidate = await pool_search.search_recipe_pool(
                user_profile,
                meal_type,
                excluded,
                used_cuisines=cuisines,
                avoid_titles=titles,
                include_stubs=False,
                max_time_minutes=max_time_minutes,
                **rung,
            )
            if not candidate:
                break
            filled.append(candidate)
            excluded.add(str(candidate["id"]))
            titles.add(candidate["title"])
            batch_only_titles.add(candidate["title"])
            if candidate.get("cuisine"):
                cuisines.append(candidate["cuisine"])

    if len(filled) < needed:
        repeats = await pool_search.find_oldest_repeat_candidates(
            user_profile,
            meal_type,
            excluded,
            needed - len(filled),
            max_time_minutes=max_time_minutes,
            avoid_titles=batch_only_titles,
        )
        filled += repeats

    return filled


async def _log_suggestions(user_id: str, options: list[dict]) -> None:
    """Logs every option actually SHOWN, whether selected or not.

    2026-07-16: this powered a 28-day "don't re-show anything shown
    recently" exclusion (history.get_recently_suggested_*) — removed same
    day, since treating "shown" as "rejected" was the real cause of a
    heavily-tested account accumulating 163 stale "avoid repeating" titles
    (see history.get_discarded_recipe_ids/get_discarded_titles, the
    permanent explicit signal that replaced it for that purpose). Hours
    later, get_recently_suggested_* came back reading this same table, now
    with a 7-day window (history.RECENTLY_SHOWN_WINDOW_DAYS) — a short
    session-scoped cooldown against immediate repeats (a live regression:
    the same fresh-generated dish twice in 34 seconds, nothing having ever
    signalled "you just showed this"), not the old blanket multi-week ban."""
    for option in options:
        await db.pool().execute(
            "INSERT INTO recipe_suggestions (user_id, recipe_id) VALUES ($1, $2)",
            user_id,
            str(option["id"]),
        )


async def _generate_and_attach_images(options: list[dict], user_id: str) -> None:
    specs = [(o["id"], o["image_prompt"], o["title"]) for o in options]
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
        "variations": option.get("variations") or [],
    }
