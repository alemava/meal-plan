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


class GenerateRecipesRequest(BaseModel):
    week_start: date
    slots: list[SlotRequest]


class SlotOptions(BaseModel):
    day: str
    meal_type: MealType
    options: list[RecipeOption]


class GenerateRecipesResponse(BaseModel):
    week_id: str
    week_start: date
    slots: list[SlotOptions]


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
