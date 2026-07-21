from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.models.recipes import RecipeOption

MealType = Literal["breakfast", "lunch", "dinner", "special"]


class SlotRequest(BaseModel):
    day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    meal_type: MealType = "dinner"
    # Free-text user craving/note (e.g. "something spicy") — the first live
    # free-text input in mesa's generation pipeline, so this is a real
    # prompt-injection surface, not just a theoretical one. Length-capped
    # here; content-checked against guardrails.DANGEROUS_TERMS before it
    # ever reaches a prompt (see fresh_generation.py).
    comment: str | None = Field(default=None, max_length=200)
    # Max total time in minutes for this meal — a soft prompt constraint
    # (like the dish-integrity rule), not a hard guardrail: not safety
    # critical, so no rejection/retry if the model's own time estimate
    # comes back a bit over.
    max_time_minutes: int | None = Field(default=None, ge=5, le=480)
    # 2026-07-20, user-requested: the first 3 breakfast slots in a batch
    # always cover one fixed category each (yogurt/toast/eggs) rather than
    # free variety — the frontend assigns this per slot index (see
    # index.html's slot-building loop); a 4th+ breakfast slot leaves this
    # unset for normal unconstrained generation. Soft preference like
    # max_time_minutes above, not a hard guardrail — see
    # generate_recipes.py's BREAKFAST_CATEGORY_KEYWORDS.
    breakfast_category: Literal["yogurt", "toast", "eggs"] | None = None


class PantryItem(BaseModel):
    name: str
    qty: float | None = Field(default=None, gt=0)
    unit: str | None = None


class GenerateRecipesRequest(BaseModel):
    week_start: date
    slots: list[SlotRequest]
    # Household-level, not per-slot (a real pantry item like rice can cover
    # any meal that uses it) — see guardrails.sanitize_pantry_ingredients for
    # the content filter and fresh_generation.py for how it reaches the
    # prompt. Capped at 40 like slots' own list fields, generous for a real
    # kitchen inventory without inviting an abuse-sized payload.
    pantry: list[PantryItem] | None = Field(default=None, max_length=40)


class SlotOptions(BaseModel):
    day: str
    meal_type: MealType
    options: list[RecipeOption]


class GenerateRecipesResponse(BaseModel):
    week_id: str
    week_start: date
    slots: list[SlotOptions]
    # len(slots) alone is ambiguous while a job is still running (partial results) — these disambiguate it.
    total_slots: int = 0
    completed_slots: int = 0


class GenerateRecipesAccepted(BaseModel):
    """202 response for the now-async POST /api/generate-recipes — the real
    result is fetched via polling GET /api/generate-recipes/{job_id}."""

    job_id: str
    status: Literal["pending"] = "pending"


class GenerationJobStatus(BaseModel):
    job_id: str
    status: Literal["pending", "running", "complete", "failed"]
    result: GenerateRecipesResponse | None = None
    error: str | None = None
