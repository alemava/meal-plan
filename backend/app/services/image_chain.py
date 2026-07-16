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
    recipe_id: str, image_prompt: str, user_id: str | None = None
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
    raised, without logging a Cloudflare attempt that never happened."""
    filename = f"{recipe_id}.jpg"

    if not await cost_status.cloudflare_image_quota_exhausted():
        try:
            image_bytes = await cloudflare.generate_image(image_prompt)
            await _log_image_call("cloudflare", True, recipe_id, user_id)
            return await storage.upload_recipe_image(filename, image_bytes)
        except Exception:
            await _log_image_call("cloudflare", False, recipe_id, user_id)

    settings = get_settings()
    if settings.image_paid_backstop and not await cost_status.get_image_backstop_disabled():
        try:
            image_bytes = await deepinfra.generate_image(image_prompt)
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


async def generate_images_for_live_options(
    specs: list[tuple[str, str]], user_id: str | None = None
) -> list[str | None]:
    """Image-text sync rule: the two live options show title+image together,
    so both images are generated synchronously, in parallel, to halve the wait
    — never queued for a request the user is actively waiting on."""
    return await asyncio.gather(
        *(
            generate_and_upload_image(recipe_id, prompt, user_id=user_id)
            for recipe_id, prompt in specs
        )
    )
