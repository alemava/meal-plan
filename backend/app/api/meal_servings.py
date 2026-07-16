from fastapi import APIRouter, Depends, HTTPException

from app.core import db
from app.core.security import get_current_user_id
from app.models.meal_servings import UpdateMealServingsRequest, UpdateMealServingsResponse

router = APIRouter(prefix="/api", tags=["recipes"])


@router.patch("/meal-servings", response_model=UpdateMealServingsResponse)
async def update_meal_servings(
    request: UpdateMealServingsRequest, user_id: str = Depends(get_current_user_id)
):
    """meal_weeks.meals[] entries never store ingredients (only recipe_id +
    people + display fields, see select_recipe.py's _write_selection) — so
    changing servings only ever needs to update this one int. hydrateMeals
    (index.html) re-derives scaled ingredient qtys and kcal from canonical
    recipe data on every load, so nothing else here needs to change for the
    new serving count to take effect.

    Uses the atomic jsonb_agg/CASE pattern (documented in CLAUDE.md for
    exactly this kind of single-field update) rather than a Python
    fetch-filter-replace: a +/- stepper UI can fire rapid repeated PATCHes,
    and this way there's no read-modify-write race, and no jsonb *parameter*
    built in Python at all (sidesteps the asyncpg double-encoding footgun
    documented in CLAUDE.md/db.py entirely). The SELECT below is pure
    validation for a clean 404 — the UPDATE below is the only real write.
    """
    exists = await db.pool().fetchval(
        "SELECT EXISTS(SELECT 1 FROM meal_weeks, jsonb_array_elements(meals) m "
        "WHERE user_id = $1 AND id = $2 AND m->>'id' = $3)",
        user_id,
        request.week_id,
        request.meal_id,
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Meal not found")

    await db.pool().execute(
        """
        UPDATE meal_weeks
        SET meals = (
          SELECT jsonb_agg(
            CASE WHEN m->>'id' = $1
            THEN m || jsonb_build_object('people', $2::int)
            ELSE m END
          )
          FROM jsonb_array_elements(meals) m
        )
        WHERE user_id = $3 AND id = $4
        """,
        request.meal_id,
        request.people,
        user_id,
        request.week_id,
    )
    return UpdateMealServingsResponse()
