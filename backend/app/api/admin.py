from fastapi import APIRouter, Depends

import pool_warmer
from app.core import db
from app.core.security import require_admin, require_internal_secret
from app.services import cloudflare, cost_status, provider_quota, recipe_audit

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


@router.post("/reembed")
async def reembed(_user_id: str = Depends(require_admin)):
    """Embed every recipe with embedding IS NULL. Run after any bulk import."""
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
