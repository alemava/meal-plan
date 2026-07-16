from fastapi import APIRouter, Depends, HTTPException

from app.core import db
from app.core.security import get_current_user_id
from app.models.discard import DiscardRecipeRequest, DiscardRecipeResponse

router = APIRouter(prefix="/api", tags=["recipes"])


@router.post("/discard-recipe", response_model=DiscardRecipeResponse)
async def discard_recipe(request: DiscardRecipeRequest, user_id: str = Depends(get_current_user_id)):
    """Explicit "don't show me this again" signal (2026-07-16) — replaces the
    old blanket 28-day "anything shown gets excluded" behaviour (see
    history.get_discarded_recipe_ids/get_discarded_titles). Permanent, no
    time window, no undiscard endpoint yet (a natural future ask if it comes
    up). Idempotent — discarding the same recipe twice is a harmless no-op."""
    recipe = await db.pool().fetchrow("SELECT id FROM recipes WHERE id = $1", request.recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    await db.pool().execute(
        "INSERT INTO recipe_discards (user_id, recipe_id) VALUES ($1, $2) "
        "ON CONFLICT (user_id, recipe_id) DO NOTHING",
        user_id,
        request.recipe_id,
    )
    return DiscardRecipeResponse()
