import asyncio

from fastapi import APIRouter, Depends

from app.api import generate_recipes
from app.core import db
from app.core.config import get_settings
from app.core.security import require_internal_secret
from app.models.generate import SlotRequest
from app.services import resend_client
from app.services.steps_generation import generate_steps

router = APIRouter(prefix="/api/internal", tags=["internal"])

# Headroom under Cloud Run's real request timeout (confirmed live via
# `gcloud run services describe` at 300s) — the worker is itself an HTTP
# request Cloud Tasks dispatches, so it's bound by the same platform limit.
# If a batch is still running at this point, whatever slots already finished
# are already durably persisted (see _persist below), so letting this raise
# and propagate to Cloud Tasks' own retry is correct: the next attempt only
# redoes what's left, not the whole batch.
BATCH_WORKER_TIMEOUT_SECONDS = 260


@router.post("/generate-steps", dependencies=[Depends(require_internal_secret)])
async def generate_steps_task(payload: dict):
    """Cloud Tasks worker target (see cloud_tasks.enqueue_steps_generation).
    Any failure here (including AIProviderExhausted from a self-imposed
    quota cutoff) is deliberately left to propagate as an unhandled
    exception -> FastAPI's default 500 -> Cloud Tasks retries per the
    queue's own policy (up to 50 attempts, 30s-600s backoff, 24h max
    duration). No caller is a browser here, so there's no CORS/UX concern
    with a plain 500 the way there was on the old synchronous endpoint."""
    recipe_id = payload["recipe_id"]

    row = await db.pool().fetchrow("SELECT * FROM recipes WHERE id = $1", recipe_id)
    if row is None:
        # Deleted between enqueue and dispatch — nothing to retry for.
        return {"status": "skipped", "reason": "recipe not found"}
    recipe = dict(row)

    if recipe["status"] != "partial":
        # Already complete (an earlier delivery succeeded, or a duplicate
        # dispatch) — idempotent no-op, not an error worth retrying.
        return {"status": "already_complete"}

    steps = await generate_steps(recipe)

    claimed = await db.pool().fetchval(
        """
        UPDATE recipes SET steps = $1::jsonb, status = 'complete'
        WHERE id = $2 AND status = 'partial' RETURNING id
        """,
        steps,
        recipe_id,
    )
    if not claimed:
        return {"status": "already_complete"}

    await resend_client.send_email(
        "Your recipe is ready!",
        f"<p>Your recipe <strong>{recipe['title']}</strong> is ready to view in mesa.</p>",
    )
    return {"status": "complete"}


@router.post("/generate-recipes-batch", dependencies=[Depends(require_internal_secret)])
async def generate_recipes_batch_task(payload: dict):
    """Cloud Tasks worker target (see
    cloud_tasks.enqueue_generate_recipes_batch). Same idempotency/retry
    philosophy as generate-steps above: any unhandled exception (including
    a timeout past BATCH_WORKER_TIMEOUT_SECONDS) propagates to a 500 and
    Cloud Tasks retries per the queue's own policy. What's different here:
    _run_generation persists each slot's result as it finishes (via
    on_slot_complete below), so a retry after a partial failure only
    regenerates whatever slots didn't complete last time, not the whole
    batch — the queue's 24h max retry duration comfortably covers even a
    "wait for tomorrow's quota reset" case for free."""
    job_id = payload["job_id"]

    row = await db.pool().fetchrow("SELECT * FROM generation_jobs WHERE id = $1", job_id)
    if row is None:
        # Deleted between enqueue and dispatch — nothing to retry for.
        return {"status": "skipped", "reason": "job not found"}
    job = dict(row)

    if job["status"] == "complete":
        return {"status": "already_complete"}

    await db.pool().execute(
        "UPDATE generation_jobs SET status = 'running', updated_at = now() "
        "WHERE id = $1 AND status = 'pending'",
        job_id,
    )

    already_complete = {int(idx): slot["options"] for idx, slot in (job["result"] or {}).items()}
    slots = [SlotRequest(**s) for s in job["slots_request"]]

    async def _persist(idx: int, day: str, meal_type: str, options: list[dict]) -> None:
        await db.pool().execute(
            """
            UPDATE generation_jobs
            SET result = coalesce(result, '{}'::jsonb) || jsonb_build_object($1::text, $2::jsonb),
                updated_at = now()
            WHERE id = $3
            """,
            str(idx),
            {
                "day": day,
                "meal_type": meal_type,
                "options": [generate_recipes._to_option_dict(o) for o in options],
            },
            job_id,
        )

    await asyncio.wait_for(
        generate_recipes._run_generation(
            job["user_id"],
            job["week_start"],
            slots,
            already_complete=already_complete,
            on_slot_complete=_persist,
        ),
        timeout=BATCH_WORKER_TIMEOUT_SECONDS,
    )

    claimed = await db.pool().fetchval(
        "UPDATE generation_jobs SET status = 'complete', updated_at = now() "
        "WHERE id = $1 AND status != 'complete' RETURNING id",
        job_id,
    )
    if not claimed:
        return {"status": "already_complete"}

    settings = get_settings()
    link = f"{settings.frontend_base_url}/?job={job_id}"
    await resend_client.send_email(
        "Your recipe options are ready!",
        f'<p>Your generated meal options are ready to view in mesa. '
        f'<a href="{link}">Open mesa</a>.</p>',
    )
    return {"status": "complete"}
