import time
from datetime import UTC, date, datetime, timedelta

from app.core import db
from app.services import cloudflare, resend_client

# One under OpenRouter's real limits (20/min, 1000/day) — deliberate safety
# margin, and blocking BEFORE attempting a call (not after a 429) means a
# user never waits for a request that's already doomed to fail. No provider
# gives us an authoritative remaining-quota number (checked live: OpenRouter
# returns zero rate-limit-* headers), so this self-count is our own
# conservative approximation, not a mirror of the provider's real state.
# Generic by provider (not hardcoded to openrouter) so the same mechanism
# covers Groq or image providers later without a schema change.
#
# Groq DOES return real rate-limit headers (unlike OpenRouter), but there
# are TWO separate Groq limits, not one — a first live check only found the
# per-minute-ish one (x-ratelimit-limit-tokens=12000, refills in ~205ms) and
# missed the real binding constraint: a genuine 100,000-tokens-PER-DAY cap,
# only surfaced by reading a live 429 body in full ("Rate limit reached...
# tokens per day (TPD): Limit 100000, Used 99964..."). A request-count-only
# proxy (950/day) is meaningless against a token-denominated daily cap — at
# the real observed average of ~1754 tokens/call, 950 requests would be
# ~1.66M tokens, wildly over the actual 100K/day limit. DAILY_TOKEN_CAPS
# below tracks the real thing directly instead of proxying it.
#
# 2026-07-15: the same request-count-vs-token-budget mismatch existed for
# the PER-MINUTE cap too, just less obviously — PER_MINUTE_CAPS["groq"]=6
# was a flat request count guessed against Groq's real 12,000 TPM limit,
# with no visibility into whether a given minute's calls were actually
# small or large. Measured live (all-time, per purpose, error-free calls):
# generate_recipes averages 1961 tokens/call (max 3387), stub_expansion
# averages 1028 (max 1493) — a batch of 15 generate_recipes-heavy calls
# could burst well past 12,000 TPM long before hitting a flat count of 6,
# while a stub_expansion-heavy minute had real unused headroom under that
# same flat "6". PER_MINUTE_TOKEN_CAPS tracks the real thing directly, same
# fix as the daily cap got. The flat per-minute request cap stays too, as a
# cheap secondary guard against Groq's separate real 30 RPM limit — raised
# from 6 to 25 (one-under-30, same "stay just under the real limit"
# philosophy as OpenRouter's 19) now that the token cap is the actual
# limiting factor for realistic traffic, not this one.
# cloudflare_text (2026-07-16) — the 2 free Cloudflare text call sites
# (pool_warmer.py's stub descriptions, recipe_audit.py's defining-ingredient
# check) share CF's ~10K Neurons/day free tier with image generation (see
# cost_status.CLOUDFLARE_IMAGE_DAILY_CAP), and were completely unlogged/
# unguarded until now — this reuses the exact same generic engine already
# built for openrouter/groq below (can_call/is_daily_blocked/handle_blocked/
# get_usage_report), since prompt_audit_log's `model LIKE 'provider:%'` shape
# fits a plain-text provider identically once log_simple_call below writes
# rows for it. Conservative round numbers, not derived from a real observed
# ceiling like Groq's — actual nightly volume is currently ~8-30 calls, this
# just stops a runaway loop well before it could threaten CF's shared budget.
DAILY_CAPS = {"openrouter": 999, "cloudflare_text": 200}
PER_MINUTE_CAPS = {"openrouter": 19, "groq": 25, "cloudflare_text": 20}
DAILY_TOKEN_CAPS = {"groq": 95000}  # one comfortably under Groq's real 100,000 TPD cap
PER_MINUTE_TOKEN_CAPS = {"groq": 10800}  # one comfortably under Groq's real 12,000 TPM cap
ALERT_THRESHOLDS = [500, 900]  # informational only — never blocks anything


async def log_simple_call(
    provider: str,
    purpose: str,
    prompt_text: str,
    completion_text: str | None,
    latency_ms: int,
    error: str | None,
) -> None:
    """ai_client._log_call's counterpart for providers outside the
    tool-calling chat_completion loop entirely — currently just Cloudflare's
    free generate_text (plain REST, no tool_calls, no token usage reported).
    Same prompt_audit_log table and `provider:purpose` model format, so
    can_call/is_daily_blocked/get_usage_report above already work unmodified
    once these rows exist."""
    await db.pool().execute(
        """
        INSERT INTO prompt_audit_log (model, prompt_text, completion_text, latency_ms, error)
        VALUES ($1, $2, $3, $4, $5)
        """,
        f"{provider}:{purpose}",
        prompt_text,
        completion_text,
        latency_ms,
        error,
    )


async def get_daily_usage(provider: str) -> int:
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return await db.pool().fetchval(
        "SELECT count(*) FROM prompt_audit_log WHERE model LIKE $1 AND created_at >= $2",
        f"{provider}:%",
        today_start,
    )


async def get_daily_token_usage(provider: str) -> int:
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return await db.pool().fetchval(
        "SELECT coalesce(sum(tokens_in + tokens_out), 0) FROM prompt_audit_log "
        "WHERE model LIKE $1 AND created_at >= $2",
        f"{provider}:%",
        today_start,
    )


async def get_minute_usage(provider: str) -> int:
    since = datetime.now(UTC) - timedelta(minutes=1)
    return await db.pool().fetchval(
        "SELECT count(*) FROM prompt_audit_log WHERE model LIKE $1 AND created_at >= $2",
        f"{provider}:%",
        since,
    )


async def get_minute_token_usage(provider: str) -> int:
    since = datetime.now(UTC) - timedelta(minutes=1)
    return await db.pool().fetchval(
        "SELECT coalesce(sum(tokens_in + tokens_out), 0) FROM prompt_audit_log "
        "WHERE model LIKE $1 AND created_at >= $2",
        f"{provider}:%",
        since,
    )


async def get_percent_used(provider: str) -> float:
    """How much of today's budget is gone, whichever metric actually governs
    this provider (request count vs. token count — see DAILY_TOKEN_CAPS'
    comment on why a provider can't just be checked both ways generically).
    0.0 for a provider with no configured cap at all. Used by pool_warmer.py
    to decide whether there's headroom left for optional quota-spending work
    beyond live user traffic, and extracted here (rather than duplicated)
    since get_usage_report below needs the exact same arithmetic."""
    if provider in DAILY_CAPS:
        return round(await get_daily_usage(provider) / DAILY_CAPS[provider] * 100, 1)
    if provider in DAILY_TOKEN_CAPS:
        return round(await get_daily_token_usage(provider) / DAILY_TOKEN_CAPS[provider] * 100, 1)
    return 0.0


async def is_daily_blocked(provider: str) -> bool:
    """True if a DAILY-denominated cap (request count or token count) is
    what's currently blocking this provider, as opposed to a per-minute cap.
    A daily cap cannot free up within any sane wait window (it resets at
    midnight UTC), so this lets a capacity-wait skip waiting entirely rather
    than burn real seconds polling for something that can't happen — a real
    cost measured live: every Groq call paid an unconditional ~12s tax on a
    day its 95k-token daily cap was already spent, since the old wait loop
    only ever polled, with no concept of which cap it was waiting on."""
    daily_cap = DAILY_CAPS.get(provider)
    daily_token_cap = DAILY_TOKEN_CAPS.get(provider)
    if daily_cap is not None and await get_daily_usage(provider) >= daily_cap:
        return True
    if daily_token_cap is not None and await get_daily_token_usage(provider) >= daily_token_cap:
        return True
    return False


async def can_call(provider: str) -> bool:
    """The single check every OpenRouter call site (sync fallback and the
    async steps worker) must pass before attempting a call. Also the check
    _wait_for_capacity() polls in ai_client.py, so the token-aware minute
    cap below directly benefits that path too, no separate wiring."""
    daily_cap = DAILY_CAPS.get(provider)
    minute_cap = PER_MINUTE_CAPS.get(provider)
    daily_token_cap = DAILY_TOKEN_CAPS.get(provider)
    minute_token_cap = PER_MINUTE_TOKEN_CAPS.get(provider)
    if daily_cap is not None and await get_daily_usage(provider) >= daily_cap:
        return False
    if minute_cap is not None and await get_minute_usage(provider) >= minute_cap:
        return False
    if daily_token_cap is not None and await get_daily_token_usage(provider) >= daily_token_cap:
        return False
    if minute_token_cap is not None and await get_minute_token_usage(provider) >= minute_token_cap:
        return False
    return True


async def _get_or_create_alert_row(provider: str) -> dict:
    today = date.today()
    row = await db.pool().fetchrow(
        "SELECT * FROM provider_daily_alerts WHERE date = $1 AND provider = $2", today, provider
    )
    if row:
        return dict(row)
    await db.pool().execute(
        "INSERT INTO provider_daily_alerts (date, provider) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        today,
        provider,
    )
    return {"sent_500": False, "sent_900": False, "sent_exhausted": False}


async def check_and_alert(provider: str) -> None:
    """Informational-only heads-up emails — never blocks a call, just gives
    early warning for the usage report. Request-count providers alert at
    fixed thresholds (500/900); token-denominated providers (Groq) alert at
    70%/90% of their cap instead, since a fixed token count wouldn't scale
    across different caps the way a fixed request count roughly does here."""
    daily_cap = DAILY_CAPS.get(provider)
    token_cap = DAILY_TOKEN_CAPS.get(provider)
    if daily_cap is None and token_cap is None:
        return
    alert_row = await _get_or_create_alert_row(provider)
    today = date.today()

    if daily_cap is not None:
        usage = await get_daily_usage(provider)
        low, high = 500, 900
        low_label, high_label = f"{low} requests", f"{high} requests"
    else:
        usage = await get_daily_token_usage(provider)
        low, high = round(token_cap * 0.7), round(token_cap * 0.9)
        low_label, high_label = f"{low} tokens (70%)", f"{high} tokens (90%)"
    cap = daily_cap if daily_cap is not None else token_cap

    if usage >= low and not alert_row["sent_500"]:
        await resend_client.send_email(
            f"mesa: {provider} at {low_label} today",
            f"<p>{provider} has used {usage} today (cutoff: {cap}).</p>",
        )
        await db.pool().execute(
            "UPDATE provider_daily_alerts SET sent_500 = true WHERE date = $1 AND provider = $2",
            today,
            provider,
        )
    if usage >= high and not alert_row["sent_900"]:
        await resend_client.send_email(
            f"mesa: {provider} at {high_label} today",
            f"<p>{provider} has used {usage} today (cutoff: {cap}). Getting close.</p>",
        )
        await db.pool().execute(
            "UPDATE provider_daily_alerts SET sent_900 = true WHERE date = $1 AND provider = $2",
            today,
            provider,
        )


async def handle_blocked(provider: str, user_id: str | None) -> None:
    """Call this when can_call() returned False and a user actually hit the
    wall — logs the event (for 'how many people saw this' reporting) and
    fires the admin alert once per day, not once per user."""
    await db.pool().execute(
        "INSERT INTO provider_exhausted_events (provider, user_id) VALUES ($1, $2)",
        provider,
        user_id,
    )
    alert_row = await _get_or_create_alert_row(provider)
    if not alert_row["sent_exhausted"]:
        today = date.today()
        await resend_client.send_email(
            f"mesa: {provider} daily cutoff reached",
            f"<p>{provider} hit its self-imposed daily cutoff today. Users are now seeing the "
            "'chef is busy' message until it resets.</p>",
        )
        await db.pool().execute(
            "UPDATE provider_daily_alerts SET sent_exhausted = true WHERE date = $1 AND provider = $2",
            today,
            provider,
        )


async def call_cloudflare_text_with_quota(purpose: str, system_prompt: str, user_prompt: str) -> str:
    """Shared can_call -> call -> log_simple_call wrapper for
    cloudflare.generate_text's 2 call sites (pool_warmer.py's stub
    descriptions, recipe_audit.py's defining-ingredient check) — both need
    the identical dance, so it lives here once instead of duplicated in each
    caller. Raises plain RuntimeError (not AIProviderExhausted, which is
    scoped to ai_client's tool-calling loop — Cloudflare here is a plain
    REST client outside it) when blocked; both callers already have a bare
    `except Exception` at their own call site that absorbs this with zero
    new error-handling code."""
    if not await can_call("cloudflare_text"):
        await handle_blocked("cloudflare_text", None)
        raise RuntimeError("cloudflare_text: daily/per-minute cap reached")

    start = time.monotonic()
    try:
        raw = await cloudflare.generate_text(system_prompt, user_prompt)
    except Exception as exc:
        await log_simple_call(
            "cloudflare_text", purpose, user_prompt, None,
            int((time.monotonic() - start) * 1000), str(exc),
        )
        raise
    await log_simple_call(
        "cloudflare_text", purpose, user_prompt, raw,
        int((time.monotonic() - start) * 1000), None,
    )
    return raw


async def get_usage_report() -> dict:
    """How many people saw the busy message, and how fast we're burning
    through the daily cutoff — for the user's own admin-facing reporting."""
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    hours_elapsed = max((datetime.now(UTC) - today_start).total_seconds() / 3600, 1 / 60)

    report = {}
    for provider, daily_cap in DAILY_CAPS.items():
        usage = await get_daily_usage(provider)
        rate_per_hour = usage / hours_elapsed

        events = await db.pool().fetch(
            "SELECT user_id FROM provider_exhausted_events WHERE provider = $1 AND occurred_at >= $2",
            provider,
            today_start,
        )
        distinct_users = {str(row["user_id"]) for row in events if row["user_id"]}
        alert_row = await _get_or_create_alert_row(provider)

        report[provider] = {
            "usage_today": usage,
            "daily_cutoff": daily_cap,
            "percent_used": await get_percent_used(provider),
            "requests_per_hour": round(rate_per_hour, 1),
            "projected_exhaustion_hour_of_day": (
                round(daily_cap / rate_per_hour, 1) if rate_per_hour > 0 else None
            ),
            "busy_message_shown_count_today": len(events),
            "distinct_users_blocked_today": len(distinct_users),
            "alerts_sent_today": {
                "500": alert_row["sent_500"],
                "900": alert_row["sent_900"],
                "exhausted": alert_row["sent_exhausted"],
            },
        }

    # Separate loop for token-denominated caps (currently just Groq) — same
    # report shape, different underlying metric, since a request count alone
    # is meaningless against a token budget (see DAILY_TOKEN_CAPS comment).
    for provider, token_cap in DAILY_TOKEN_CAPS.items():
        tokens_used = await get_daily_token_usage(provider)
        rate_per_hour = tokens_used / hours_elapsed

        events = await db.pool().fetch(
            "SELECT user_id FROM provider_exhausted_events WHERE provider = $1 AND occurred_at >= $2",
            provider,
            today_start,
        )
        distinct_users = {str(row["user_id"]) for row in events if row["user_id"]}
        alert_row = await _get_or_create_alert_row(provider)

        report[provider] = {
            "tokens_used_today": tokens_used,
            "daily_token_cutoff": token_cap,
            "percent_used": await get_percent_used(provider),
            "tokens_per_hour": round(rate_per_hour, 1),
            "projected_exhaustion_hour_of_day": (
                round(token_cap / rate_per_hour, 1) if rate_per_hour > 0 else None
            ),
            "busy_message_shown_count_today": len(events),
            "distinct_users_blocked_today": len(distinct_users),
            "alerts_sent_today": {
                "500": alert_row["sent_500"],
                "900": alert_row["sent_900"],
                "exhausted": alert_row["sent_exhausted"],
            },
        }
    return report
