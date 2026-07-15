from fastapi import APIRouter, Depends, HTTPException

import pool_warmer
from app.core import db
from app.core.security import require_admin, require_internal_secret
from app.services import (
    cloudflare,
    cost_status,
    provider_quality,
    provider_quota,
    provider_status,
    recipe_audit,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/pool-warm", dependencies=[Depends(require_internal_secret)])
async def pool_warm():
    """ADR 4 — triggered nightly by Cloud Scheduler (see pool_warmer.py)."""
    return await pool_warmer.run_pool_warmer()


@router.post("/recipe-audit", dependencies=[Depends(require_internal_secret)])
async def recipe_audit_endpoint():
    """Part C — on-demand trigger for the same audit run_pool_warmer already
    piggybacks nightly. Useful right after a manual data fix/backfill,
    without waiting for the next pool-warm cycle."""
    return await recipe_audit.run_recipe_audit()


@router.get("/cost-status", dependencies=[Depends(require_internal_secret)])
async def get_cost_status():
    """Reads usage/limit-hit signals, flips the generation kill-switch if the
    daily call threshold is breached, and alerts via Resend on either that or
    a sustained image-provider backlog. Safe to call repeatedly (e.g. from a
    Cloud Scheduler job) — only sends an alert on a state transition."""
    return await cost_status.check_cost_status()


@router.get("/provider-usage-report")
async def provider_usage_report(_user_id: str = Depends(require_admin)):
    """Per-provider daily usage vs. our self-imposed cutoff, consumption
    rate, and how many users have actually hit the 'chef is busy' message
    today — provider_quota.py owns the underlying tracking."""
    return await provider_quota.get_usage_report()


@router.get("/provider-quality-report")
async def provider_quality_report(since_days: int = 30, _user_id: str = Depends(require_admin)):
    """Reliability (attempt success/latency) + content-quality (defect rate,
    tiered by trustworthiness) per provider — the data behind the "is
    Groq/OpenRouter good enough for prod" decision. Read alongside
    provider-usage-report above (capacity is a separate axis from quality)."""
    return await provider_quality.get_provider_quality_scorecard(since_days)


@router.get("/recipe-audit-findings")
async def recipe_audit_findings(
    resolved: bool = False,
    provider: str | None = None,
    tier: str | None = None,
    check_name: str | None = None,
    limit: int = 50,
    offset: int = 0,
    _user_id: str = Depends(require_admin),
):
    """Filterable view of recipe_audit_findings + a backlog-size summary.
    tier is one of deterministic/heuristic/semantic (see provider_quality.py);
    check_name takes precedence over tier if both are given."""
    try:
        return await recipe_audit.list_findings(resolved, provider, tier, check_name, limit, offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/recipe-audit-findings/{finding_id}/resolve")
async def resolve_recipe_audit_finding(
    finding_id: str, note: str | None = None, _user_id: str = Depends(require_admin)
):
    """Until this endpoint existed, resolving a finding required a manual
    Supabase edit — there was no code path that ever set resolved=true."""
    result = await recipe_audit.resolve_finding(finding_id, note)
    if result is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return result


@router.get("/provider-status")
async def get_provider_status(_user_id: str = Depends(require_admin)):
    """Current enabled/disabled state for every provider ever manually
    toggled (see provider_status.py for why this is manual-only, never
    auto-flipped by quality signals)."""
    return await provider_status.get_all_statuses()


@router.post("/provider-status/{provider}")
async def set_provider_status(
    provider: str, enabled: bool, reason: str | None = None, _user_id: str = Depends(require_admin)
):
    """The actual lever for a go/no-go call on a provider — before this
    endpoint existed, even a clear-cut "turn OpenRouter off" decision had
    no way to act on it short of a code change and redeploy."""
    return await provider_status.set_enabled(provider, enabled, reason)


@router.post("/reembed")
async def reembed(_user_id: str = Depends(require_admin)):
    """Embed every recipe with embedding IS NULL. Run after any bulk import.
    Also the manual runbook step for a model swap: null out every recipe's
    embedding first, THEN call this — it truncates query_embedding_cache
    (pool_search.py) unconditionally so no stale query-text vector from the
    old model lingers and gets compared against the new one."""
    await db.pool().execute("TRUNCATE query_embedding_cache")
    rows = await db.pool().fetch(
        "SELECT id, title, brief_description FROM recipes WHERE embedding IS NULL"
    )

    updated = 0
    errors = []
    for row in rows:
        source_text = f"{row['title']}. {row['brief_description'] or ''}".strip()
        try:
            vector = await cloudflare.embed_text(source_text)
            await db.pool().execute(
                "UPDATE recipes SET embedding = $1::vector WHERE id = $2",
                cloudflare.vector_literal(vector),
                row["id"],
            )
            updated += 1
        except Exception as exc:  # noqa: BLE001 — collect per-row failures, don't abort the batch
            errors.append({"recipe_id": str(row["id"]), "error": str(exc)})

    return {"found": len(rows), "updated": updated, "failed": len(errors), "errors": errors}
