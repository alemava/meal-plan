import asyncio

from app.core import db
from app.core.config import get_settings
from app.services import cloudflare, cost_status, deepinfra, storage


async def _log_image_call(
    provider: str, success: bool, recipe_id: str | None, user_id: str | None
) -> None:
    await db.pool().execute(
        "INSERT INTO image_generation_log (recipe_id, user_id, provider, success) VALUES ($1, $2, $3, $4)",
        recipe_id,
        user_id,
        provider,
        success,
    )


async def generate_and_upload_image(
    recipe_id: str,
    image_prompt: str,
    user_id: str | None = None,
    title: str | None = None,
    filename_override: str | None = None,
) -> str | None:
    """The provider chain: Cloudflare -> DeepInfra (paid backstop, gated by
    IMAGE_PAID_BACKSTOP + the monthly spend cap) -> queue. Last resort if
    both fail on a live request: enqueue and return None so the caller can
    show a placeholder — never block or fail the request over an image.
    Every attempt (success or failure) is logged here, the single call site,
    so recipe_id/user_id traceability can't be bypassed by a future direct
    call to a provider client.

    Cloudflare itself is gated by a proactive daily-attempt cap (2026-07-16,
    see cost_status.cloudflare_image_quota_exhausted) — once exhausted this
    skips straight to the DeepInfra branch exactly as if Cloudflare had just
    raised, without logging a Cloudflare attempt that never happened.

    `title` is prepended to `image_prompt` here (2026-07-18), the one call
    site every image request goes through, so it happens deterministically
    regardless of whether the model's own image_prompt text remembered to
    disambiguate the dish (see ai_client.py's image_prompt guidance for a
    real example this fixes: "Tortilla de Patatas con Aceitunas" rendered as
    a Mexican wrap, since "tortilla" alone reads as the wrap/flatbread to an
    image model without either signal).

    `filename_override` (2026-07-20, breakfast/lunch/dinner variation
    photos): `recipe_id` always stays the REAL recipe UUID — both
    image_generation_log.recipe_id and image_generation_queue.recipe_id are
    FK-constrained against `recipes`, so a synthetic per-variation id would
    fail those inserts outright. The storage FILENAME is the only thing
    that needs to differ, so a variation's photo doesn't overwrite the base
    recipe's own `<recipe_id>.jpg` object in Supabase Storage."""
    filename = f"{filename_override or recipe_id}.jpg"
    prompt = f"{title}. {image_prompt}" if title else image_prompt

    if not await cost_status.cloudflare_image_quota_exhausted():
        try:
            image_bytes = await cloudflare.generate_image(prompt)
            await _log_image_call("cloudflare", True, recipe_id, user_id)
            return await storage.upload_recipe_image(filename, image_bytes)
        except Exception:
            await _log_image_call("cloudflare", False, recipe_id, user_id)

    settings = get_settings()
    if settings.image_paid_backstop and not await cost_status.get_image_backstop_disabled():
        try:
            image_bytes = await deepinfra.generate_image(prompt)
            await _log_image_call("deepinfra", True, recipe_id, user_id)
            return await storage.upload_recipe_image(filename, image_bytes)
        except Exception:
            await _log_image_call("deepinfra", False, recipe_id, user_id)

    await db.pool().execute(
        "INSERT INTO image_generation_queue (recipe_id, image_prompt) VALUES ($1, $2)",
        recipe_id,
        image_prompt,
    )
    return None


async def generate_stub_image_cloudflare_only(
    recipe_id: str, image_prompt: str, title: str | None = None
) -> str | None:
    """Pool-warmer's own image call (2026-07-20) — deliberately NOT the full
    generate_and_upload_image chain, because that chain's whole point is to
    fall through to paid DeepInfra when Cloudflare is unavailable, which is
    correct for a live user waiting on a result but wrong here: pool_warmer's
    entire premise is zero paid cost, so a stub that can't get a free image
    tonight should simply stay without one (image-sweep already excludes
    status='stub' rows, so this never silently escalates to paid later
    either).

    No proactive cap check here either — unlike generate_and_upload_image,
    which still gates on cost_status.cloudflare_image_quota_exhausted (a
    calibrated ESTIMATE) for the live path. This calls Cloudflare directly
    and lets cloudflare.CloudflareImageQuotaExhausted (the real 4006 signal)
    propagate to the caller, so pool_warmer can react to the actual state of
    today's allocation instead of our own guess at it."""
    prompt = f"{title}. {image_prompt}" if title else image_prompt
    try:
        image_bytes = await cloudflare.generate_image(prompt)
    except cloudflare.CloudflareImageQuotaExhausted:
        await _log_image_call("cloudflare", False, recipe_id, None)
        raise
    except Exception:
        await _log_image_call("cloudflare", False, recipe_id, None)
        return None
    await _log_image_call("cloudflare", True, recipe_id, None)
    return await storage.upload_recipe_image(f"{recipe_id}.jpg", image_bytes)


async def generate_images_for_live_options(
    specs: list[tuple[str, str, str]], user_id: str | None = None
) -> list[str | None]:
    """Image-text sync rule: the two live options show title+image together,
    so both images are generated synchronously, in parallel, to halve the wait
    — never queued for a request the user is actively waiting on."""
    return await asyncio.gather(
        *(
            generate_and_upload_image(recipe_id, prompt, user_id=user_id, title=title)
            for recipe_id, prompt, title in specs
        )
    )


# How far back the sweep looks. A genuinely-fresh orphan (worker frozen mid-
# image) is minutes old; this window is generous enough to catch anything
# recent while NOT retrying an ancient, permanently-failing image forever
# (e.g. a prompt a provider keeps refusing) and burning budget every run.
SWEEP_MAX_AGE_DAYS = 7
SWEEP_BATCH_LIMIT = 12


async def sweep_orphaned_images() -> dict:
    """Durable, request-lifecycle-INDEPENDENT backstop for the orphaned-image
    class of bug (2026-07-20). Every in-request mechanism — generate_recipes'
    background image_tasks, internal.py's pre-complete backfill — lives inside
    a Cloud Run request bounded by timeouts, so a worker freeze or a timeout
    mid-generation can always drop an image ('Arroz con Pollo', 'Grilled
    Vegetable Panini', 'Grilled Sea Bass' all hit this over three days). This
    runs on Cloud Scheduler instead, so it CANNOT be cut off by a request
    ending: any recipe left with an image_prompt but no image_url can never
    stay orphaned longer than one schedule interval. Also drains
    image_generation_queue (image_chain's last-resort insert, which otherwise
    had no consumer at all). Bounded per run so a backlog drains over several
    runs rather than one giant burst."""
    rows = await db.pool().fetch(
        """
        SELECT id, title, image_prompt FROM recipes
        WHERE image_url IS NULL
          AND image_prompt IS NOT NULL
          AND status <> 'stub'
          AND created_at >= now() - ($1 || ' days')::interval
        ORDER BY created_at DESC
        LIMIT $2
        """,
        str(SWEEP_MAX_AGE_DAYS),
        SWEEP_BATCH_LIMIT,
    )
    if not rows:
        return {"orphans_found": 0, "generated": 0}

    async def _fill_one(row) -> bool:
        url = await generate_and_upload_image(str(row["id"]), row["image_prompt"], title=row["title"])
        if not url:
            return False
        await db.pool().execute("UPDATE recipes SET image_url = $1 WHERE id = $2", url, row["id"])
        await db.pool().execute("DELETE FROM image_generation_queue WHERE recipe_id = $1", row["id"])
        return True

    results = await asyncio.gather(*(_fill_one(row) for row in rows), return_exceptions=True)
    generated = sum(1 for r in results if r is True)
    return {"orphans_found": len(rows), "generated": generated}
