from fastapi import APIRouter, Depends

from app.core import db
from app.core.security import require_internal_secret
from app.services import resend_client
from app.services.steps_generation import generate_steps

router = APIRouter(prefix="/api/internal", tags=["internal"])


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
