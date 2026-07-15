import asyncio
from datetime import UTC, datetime, timedelta

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

# The generate-recipes-batch queue is configured with max-attempts=unlimited
# specifically so maxRetryDuration (24h) governs, not a fixed attempt count —
# a job stuck on a daily-exhausted provider needs to survive past midnight
# UTC for the reset to ever get a chance. This cutoff sits comfortably under
# that 24h queue ceiling so the job always reaches an explicit 'failed'
# itself, with an email, instead of just running out of queue retries with
# no one ever told.
JOB_AGE_CUTOFF_HOURS = 20


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

    steps, steps_provider = await generate_steps(recipe)

    claimed = await db.pool().fetchval(
        """
        UPDATE recipes SET steps = $1::jsonb, status = 'complete', steps_source = $2
        WHERE id = $3 AND status = 'partial' RETURNING id
        """,
        steps,
        steps_provider,
        recipe_id,
    )
    if not claimed:
        return {"status": "already_complete"}

    await resend_client.send_email(
        "Your recipe is ready!",
        f"<p>Your recipe <strong>{recipe['title']}</strong> is ready to view in mesa.</p>",
    )
    return {"status": "complete"}


async def _fail_job(job_id: str, error: str) -> dict | None:
    """Shared claim idiom (same as the complete-claim below) so the
    terminal-failure email only ever fires once even if this races another
    attempt — 'failed' is a terminal state, same footing as 'complete'."""
    return await db.pool().fetchrow(
        "UPDATE generation_jobs SET status = 'failed', error = $2, updated_at = now() "
        "WHERE id = $1 AND status NOT IN ('complete', 'failed') RETURNING *",
        job_id,
        error,
    )


@router.post("/generate-recipes-batch", dependencies=[Depends(require_internal_secret)])
async def generate_recipes_batch_task(payload: dict):
    """Cloud Tasks worker target (see
    cloud_tasks.enqueue_generate_recipes_batch). Same idempotency/retry
    philosophy as generate-steps above: any unhandled exception (including
    a timeout past BATCH_WORKER_TIMEOUT_SECONDS, or JobSlotsIncomplete when
    a slot came back empty) propagates to a 500 and Cloud Tasks retries per
    the queue's own policy (max-attempts=unlimited, so maxRetryDuration=24h
    is what actually governs). _run_generation persists each slot's result
    as it finishes (via on_slot_complete below), so a retry after a partial
    failure only regenerates whatever slots didn't complete last time, not
    the whole batch."""
    job_id = payload["job_id"]

    row = await db.pool().fetchrow("SELECT * FROM generation_jobs WHERE id = $1", job_id)
    if row is None:
        # Deleted between enqueue and dispatch — nothing to retry for.
        return {"status": "skipped", "reason": "job not found"}
    job = dict(row)

    if job["status"] in ("complete", "failed"):
        return {"status": "already_" + job["status"]}

    # Age cutoff BEFORE the 'running' flip: give up explicitly (with an
    # email showing whatever partial progress exists) rather than let Cloud
    # Tasks eventually exhaust its own retry window with no one ever told.
    # Returning 2xx here is deliberate — a 500 would just keep the retry
    # train running for a job we've already decided is done retrying.
    age = datetime.now(UTC) - job["created_at"]
    if age > timedelta(hours=JOB_AGE_CUTOFF_HOURS):
        claimed = await _fail_job(
            job_id, "Timed out waiting for available generation capacity."
        )
        if claimed:
            result = job["result"] or {}
            total = len(job["slots_request"])
            done = sum(1 for slot in result.values() if slot.get("options"))
            await resend_client.send_email(
                "Some of your recipe options couldn't be prepared",
                f"<p>We managed to prepare {done} of {total} meals before running out of time. "
                f'<a href="{get_settings().frontend_base_url}/?job={job_id}">Open mesa</a> to see '
                "what's ready, or try generating again.</p>",
            )
        return {"status": "failed_terminal"}

    await db.pool().execute(
        "UPDATE generation_jobs SET status = 'running', updated_at = now() "
        "WHERE id = $1 AND status = 'pending'",
        job_id,
    )

    # Skip slots that were persisted empty (both providers down and the
    # pool-fill ladder found nothing safe) — those are exactly what a retry
    # needs to redo, not treat as done. The jsonb merge in _persist below
    # overwrites the key on a successful retry, healing the entry in place.
    already_complete = {
        int(idx): slot["options"] for idx, slot in (job["result"] or {}).items() if slot.get("options")
    }
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

    try:
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
    except (generate_recipes.JobSlotsIncomplete, TimeoutError) as exc:
        # Status stays 'running' (not failed — only the age cutoff above
        # decides that) so this re-raises to 500 and Cloud Tasks retries,
        # redoing only whatever didn't complete. Persisting the last error
        # just makes a stuck job debuggable without changing its state.
        await db.pool().execute(
            "UPDATE generation_jobs SET error = $2, updated_at = now() WHERE id = $1", job_id, repr(exc)
        )
        raise

    claimed = await db.pool().fetchval(
        "UPDATE generation_jobs SET status = 'complete', updated_at = now() "
        "WHERE id = $1 AND status NOT IN ('complete', 'failed') RETURNING id",
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
