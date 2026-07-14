from fastapi import APIRouter, Depends, HTTPException

from app.core import db
from app.core.security import get_current_user_id
from app.models.recipes import Ingredient
from app.models.select import SelectRecipeRequest, SelectRecipeResponse
from app.services import cloud_tasks, guardrails, provider_quota, scaling
from app.services.profile import load_profile

router = APIRouter(prefix="/api", tags=["recipes"])

PREPARING_MESSAGE = "The chef is preparing this recipe — check back in a few minutes, we'll let you know when it's ready!"
BUSY_MESSAGE = (
    "Our chef is very busy right now and can't take any more orders — please check back later."
)

DAY_LABELS = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}


@router.post("/select-recipe", response_model=SelectRecipeResponse)
async def select_recipe(request: SelectRecipeRequest, user_id: str = Depends(get_current_user_id)):
    recipe = await db.pool().fetchrow("SELECT * FROM recipes WHERE id = $1", request.recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    if recipe["status"] == "stub":
        raise HTTPException(
            status_code=409, detail="Recipe is still a stub — expand it before selecting"
        )

    recipe = dict(recipe)
    user_profile = await load_profile(user_id)
    target_serves = request.target_serves or user_profile.household_size

    ingredients = [Ingredient(**i) for i in recipe["ingredients"]]
    scaled = scaling.scale_recipe_ingredients(
        ingredients,
        base_serves=recipe["base_serves"] or 2,
        target_serves=target_serves,
        include_optional=request.include_optional,
    )

    # The meal goes into the week immediately — ingredients/image/kcal/time
    # don't need steps to exist. Only steps generation is deferred.
    await _write_selection(user_id, request, recipe, target_serves)

    message = None
    if recipe["status"] == "partial":
        message = await _kick_off_steps_generation(str(recipe["id"]), user_id)

    return SelectRecipeResponse(
        recipe_id=str(recipe["id"]),
        title=recipe["title"],
        brief_description=recipe["brief_description"],
        steps=recipe["steps"] or [],
        ingredients=scaled,
        image_url=recipe.get("image_url"),
        time=recipe.get("time"),
        kcal=recipe.get("kcal"),
        steps_ready=recipe["status"] == "complete",
        message=message,
    )


async def _kick_off_steps_generation(recipe_id: str, user_id: str) -> str:
    """Deferred step generation (ADR 2), now async via Cloud Tasks instead of
    blocking the request. Quota is checked here — BEFORE enqueueing — purely
    to give the user accurate, immediate feedback ("busy" vs "preparing");
    the task is enqueued either way, since Cloud Tasks' own retry/backoff
    means it self-heals once quota frees up without needing the user to
    reselect. The synchronous-generation design this replaced flipped
    status before steps existed, which could strand a recipe 'complete'
    with no steps if generation failed mid-flight — no longer applicable
    since the worker only flips status after steps are actually written."""
    busy = not await provider_quota.can_call("openrouter")
    if busy:
        await provider_quota.handle_blocked("openrouter", user_id)

    try:
        await cloud_tasks.enqueue_steps_generation(recipe_id, user_id)
    except Exception:
        # Never let an enqueue failure crash the whole select-recipe request
        # (the meal is already saved by this point) — the recipe just stays
        # 'partial' and a future select-recipe attempt on it retries the
        # enqueue, same self-healing philosophy as everything else today.
        return PREPARING_MESSAGE

    return BUSY_MESSAGE if busy else PREPARING_MESSAGE


async def _write_selection(
    user_id: str, request: SelectRecipeRequest, recipe: dict, target_serves: int
) -> None:
    """Step 24/25: meal_weeks only gets a row on selection, never on generation."""
    # id must be unique per (day, meal_type), not just per day — a real bug
    # found live: using just the day code meant selecting a lunch for "mon"
    # matched and silently overwrote an existing "mon" dinner (same id),
    # since a day can have several meal slots (breakfast/lunch/dinner). The
    # frontend only ever treats meal.id as an opaque unique key (lookup/nav/
    # timer-keying) and groups by `label` for day-overview, never assumes
    # id == day code, so this is safe to change with no frontend changes.
    entry_id = f"{request.day}-{request.meal_type}"
    entry = {
        "id": entry_id,
        "label": DAY_LABELS[request.day],
        "recipe_id": str(recipe["id"]),
        "people": target_serves,
        "meal_type": request.meal_type,
        "avail": [],
        "remaining": [],
        # The frontend reads meal.time/meal.kcal directly off this meal_weeks
        # entry (not a live join to recipes), so these must be copied over
        # here or they'll never actually render in the meal plan.
        "time": recipe.get("time"),
        "kcal": recipe.get("kcal"),
    }

    monday = guardrails.normalise_to_monday(
        request.week_start
    )  # guardrail 19(d) — never trust the caller

    existing = await db.pool().fetchrow(
        "SELECT meals FROM meal_weeks WHERE user_id = $1 AND id = $2", user_id, request.week_id
    )
    meals = list(existing["meals"]) if existing and existing["meals"] else []
    meals = [m for m in meals if m.get("id") != entry_id] + [entry]

    await db.pool().execute(
        """
        INSERT INTO meal_weeks (id, user_id, week_start, meals)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (user_id, id) DO UPDATE SET meals = $4::jsonb
        """,
        request.week_id,
        user_id,
        monday,
        meals,
    )

    await db.pool().execute(
        "UPDATE recipes SET usage_count = usage_count + 1 WHERE id = $1", recipe["id"]
    )
    await db.pool().execute(
        "INSERT INTO user_recipe_history (user_id, recipe_id, used_on, week_id) VALUES ($1, $2, CURRENT_DATE, $3)",
        user_id,
        recipe["id"],
        request.week_id,
    )
