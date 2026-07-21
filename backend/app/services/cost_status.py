from datetime import UTC, datetime, timedelta

from app.core import db
from app.core.config import get_settings
from app.services import resend_client

# Sanity/runaway-loop guard, not a free-tier capacity warning (2026-07-16):
# text generation moved to a paid-by-default waterfall (openrouter_paid ->
# deepinfra, see ai_client.py) with no free-tier ceiling left to protect.
# 700/day is just a "this looks like unexpectedly heavy usage, go check"
# tripwire now — tune freely, it isn't measuring anything real anymore.
DAILY_CALL_SOFT_LIMIT = 700

# Step 27's sporadic-vs-sustained split for the image provider chain.
SUSTAINED_LIMIT_HIT_DAYS_THRESHOLD = 3
SUSTAINED_QUEUE_BACKLOG_HOURS = 48

# DeepInfra is a real paid-per-call backstop (no free tier) — this is actual
# governance on it, not just a failure-count monitor. $0.0005/image at
# 1024x512 per DeepInfra's own pricing; $5/month is a generous cap for this
# project's scale, tunable.
DEEPINFRA_COST_PER_IMAGE_USD = 0.0005
MONTHLY_DEEPINFRA_CAP_USD = 5.0

# Proactive brake (2026-07-16) — image_chain.py's Cloudflare->DeepInfra chain
# was 100% reactive until now (attempt Cloudflare, only fall back if it
# raises). Cloudflare's response carries no per-call Neuron cost (checked:
# cloudflare.py only ever reads image bytes or a plain error), so this counts
# call attempts, not real Neurons — a known simplification, not false
# precision. The cap itself IS real-Neuron-derived, though: measured live via
# a single isolated generate_image call (2026-07-16, 08:17:33-35 UTC) cross-
# checked against the account's real per-minute Neuron report — 173 Neurons
# for that one call, not the ~53 a same-day aggregate (1,380 Neurons / 26
# logged calls on 2026-07-15) would have suggested. Trusting the clean
# single-call measurement over the aggregate, since the aggregate implies
# some Cloudflare image spend that day wasn't captured by this table's count
# (same untracked-direct-test-call gap this project already hit once for
# DeepInfra's usage tracking). 10,000 Neurons/day ÷ 173 ≈ 58 — the full
# theoretical ceiling if 100% of the shared free tier went to images alone,
# no margin reserved for the 2 free text call sites in provider_quota.py's
# cloudflare_text cap (user's explicit choice over a more conservative
# number). Real measurement, but still just one sample — revisit if a second
# controlled test disagrees.
#
# 2026-07-20 — recalibrated for the flux-1-schnell -> flux-2-klein-9b switch
# (see cloudflare.py): klein-9b is a Black Forest Labs PARTNER model, not a
# first-party Cloudflare one, and burns through the daily free allocation
# much faster — confirmed live: 4 real image generations (200s) exhausted
# the entire 10,000 Neuron/day budget outright (the 5th attempt got "you
# have used up your daily free allocation," Cloudflare error code 4006).
# That implies ~2,500 Neurons/image, ~14x flux-1-schnell's 173. 10,000 ÷
# 2,500 ≈ 4 — capped at 3 here for margin, since this is one day's rough
# aggregate, not the clean isolated single-call measurement the 173 number
# above was. Revisit once a controlled single-call test is possible (the
# quota needs to reset first) — same rigor this constant originally had.
CLOUDFLARE_IMAGE_DAILY_CAP = 3


async def _get_flag(key: str) -> bool:
    row = await db.pool().fetchrow("SELECT value FROM system_flags WHERE key = $1", key)
    return bool(row["value"]) if row else False


async def _set_flag(key: str, value: bool) -> None:
    await db.pool().execute(
        """
        INSERT INTO system_flags (key, value, updated_at) VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()
        """,
        key,
        value,
    )


async def get_generation_disabled() -> bool:
    return await _get_flag("generation_disabled")


async def get_image_backstop_disabled() -> bool:
    return await _get_flag("image_backstop_disabled")


async def _today_cloudflare_image_attempts() -> int:
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return await db.pool().fetchval(
        "SELECT count(*) FROM image_generation_log WHERE provider = 'cloudflare' AND occurred_at >= $1",
        today_start,
    )


async def cloudflare_image_quota_exhausted() -> bool:
    """image_chain.py's proactive gate — checked BEFORE calling
    cloudflare.generate_image, not after it fails. True/False attempts both
    count (a failed attempt still likely spent real Neurons on Cloudflare's
    side), erring conservative rather than overcounting headroom."""
    return await _today_cloudflare_image_attempts() >= CLOUDFLARE_IMAGE_DAILY_CAP


async def _monthly_deepinfra_spend() -> float:
    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    count = await db.pool().fetchval(
        """
        SELECT count(*) FROM image_generation_log
        WHERE provider = 'deepinfra' AND success = true AND occurred_at >= $1
        """,
        month_start,
    )
    return count * DEEPINFRA_COST_PER_IMAGE_USD


async def _today_call_count() -> int:
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return await db.pool().fetchval(
        "SELECT count(*) FROM prompt_audit_log WHERE created_at >= $1", today_start
    )


async def _image_provider_health() -> dict:
    since = datetime.now(UTC) - timedelta(days=30)
    limit_hit_days = await db.pool().fetchval(
        """
        SELECT count(DISTINCT date_trunc('day', occurred_at))
        FROM image_generation_log WHERE success = false AND occurred_at >= $1
        """,
        since,
    )

    backlog_cutoff = datetime.now(UTC) - timedelta(hours=SUSTAINED_QUEUE_BACKLOG_HOURS)
    stale_pending = await db.pool().fetchval(
        "SELECT count(*) FROM image_generation_queue WHERE status = 'pending' AND created_at < $1",
        backlog_cutoff,
    )

    sustained = limit_hit_days > SUSTAINED_LIMIT_HIT_DAYS_THRESHOLD or stale_pending > 0
    return {
        "limit_hit_days_last_30": limit_hit_days,
        "stale_pending_queue_items": stale_pending,
        "sustained": sustained,
    }


async def check_cost_status() -> dict:
    settings = get_settings()
    today_calls = await _today_call_count()
    image_health = await _image_provider_health()
    deepinfra_spend = await _monthly_deepinfra_spend()
    cloudflare_image_attempts = await _today_cloudflare_image_attempts()

    text_over_threshold = today_calls >= DAILY_CALL_SOFT_LIMIT
    should_disable = text_over_threshold
    currently_disabled = await get_generation_disabled()

    if should_disable and not currently_disabled:
        await _set_flag("generation_disabled", True)
        await resend_client.send_email(
            "mesa: AI generation disabled — daily call threshold reached",
            f"<p>Today's AI text calls ({today_calls}) reached the soft limit "
            f"({DAILY_CALL_SOFT_LIMIT}). Generation is now disabled until reset.</p>"
            "<p>Text generation is paid-by-default now (openrouter_paid/deepinfra) — this "
            "threshold is a runaway-loop tripwire, not a capacity ceiling. Check "
            "prompt_audit_log for what's driving the volume, or raise "
            "DAILY_CALL_SOFT_LIMIT in cost_status.py if this was expected usage.</p>",
        )
    elif not should_disable and currently_disabled:
        await _set_flag("generation_disabled", False)

    deepinfra_over_cap = deepinfra_spend >= MONTHLY_DEEPINFRA_CAP_USD
    deepinfra_currently_disabled = await get_image_backstop_disabled()
    if deepinfra_over_cap and not deepinfra_currently_disabled:
        await _set_flag("image_backstop_disabled", True)
        await resend_client.send_email(
            "mesa: DeepInfra image backstop disabled — monthly cap reached",
            f"<p>Estimated DeepInfra spend this month (${deepinfra_spend:.2f}) reached "
            f"the ${MONTHLY_DEEPINFRA_CAP_USD:.2f} cap. The paid image backstop is now "
            "disabled until next month — Cloudflare failures will go straight to the "
            "placeholder/queue instead.</p>"
            f"<p>Raise <code>MONTHLY_DEEPINFRA_CAP_USD</code> in cost_status.py if this "
            "was expected usage, not a runaway loop.</p>",
        )
    elif not deepinfra_over_cap and deepinfra_currently_disabled:
        await _set_flag("image_backstop_disabled", False)

    if image_health["sustained"]:
        await resend_client.send_email(
            "mesa: image provider chain under sustained load",
            f"<p>{image_health['limit_hit_days_last_30']} limit-hit days in the last 30, "
            f"{image_health['stale_pending_queue_items']} queue items pending >"
            f"{SUSTAINED_QUEUE_BACKLOG_HOURS}h.</p>"
            "<p>DeepInfra (the paid backstop) may itself be capped or failing — check "
            "the monthly spend cap and DeepInfra's own status.</p>",
        )

    return {
        "ai_provider": settings.ai_provider,
        "today_text_calls": today_calls,
        "daily_call_soft_limit": DAILY_CALL_SOFT_LIMIT,
        "generation_disabled": should_disable,
        "text_paid_mode": settings.text_paid_mode,
        "image_paid_backstop": settings.image_paid_backstop,
        "image_backstop_disabled": deepinfra_over_cap,
        "deepinfra_monthly_spend_usd": round(deepinfra_spend, 4),
        "deepinfra_monthly_cap_usd": MONTHLY_DEEPINFRA_CAP_USD,
        "image_provider_health": image_health,
        "cloudflare_image_attempts_today": cloudflare_image_attempts,
        "cloudflare_image_daily_cap": CLOUDFLARE_IMAGE_DAILY_CAP,
    }
