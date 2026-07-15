"""Manual, per-provider kill switch (production-readiness Part 4, 2026-07-15).

Same key/value-with-audit-fields shape as cost_status.py's system_flags, but
NOT the same table — system_flags.value is boolean-only, with nowhere to
put the "reason" the plan's own endpoint asks for. Same reasoning as
recipe_audit_watermark's own separate table earlier this session: reuse the
established idiom, not the literal shared table when its column shape
doesn't fit.

Unlike cost_status.py's kill-switches, this one deliberately does NOT
auto-flip in either direction. cost_status.py's signal (today's call/token
count) is unambiguous, real-time, and resets on a predictable schedule
(midnight UTC) — safe to auto-flip both ways. A quality signal is
structurally different: it's lagging (only known after the audit runs) and
its richest component (the semantic check) has a measured precision around
33% even on curated candidates (see provider_quality.py). Auto-disabling a
provider on that basis risks silently starving a fine provider based on
noise — exactly what recipe_audit.py already avoids by never auto-fixing
content. So: manual only, both directions, for now. If this is ever
automated, it should use the reliability signal specifically (real-time,
trustworthy, same caliber as the cost data) and be asymmetric: auto-disable
only, never auto-re-enable — "the provider stopped erroring so much in the
last hour" is a much noisier basis for silently resuming real traffic than
"midnight UTC arrived."
"""

from app.core import db


async def is_enabled(provider: str) -> bool:
    row = await db.pool().fetchrow(
        "SELECT enabled FROM provider_status WHERE provider = $1", provider
    )
    # No row yet = never manually touched = enabled (the historical default
    # before this mechanism existed at all).
    return bool(row["enabled"]) if row else True


async def set_enabled(provider: str, enabled: bool, reason: str | None = None) -> dict:
    row = await db.pool().fetchrow(
        """
        INSERT INTO provider_status (provider, enabled, reason, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (provider) DO UPDATE SET enabled = $2, reason = $3, updated_at = now()
        RETURNING provider, enabled, reason, updated_at
        """,
        provider,
        enabled,
        reason,
    )
    return dict(row)


async def get_all_statuses() -> list[dict]:
    rows = await db.pool().fetch(
        "SELECT provider, enabled, reason, updated_at FROM provider_status ORDER BY provider"
    )
    return [dict(r) for r in rows]
