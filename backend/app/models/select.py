from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.models.recipes import Ingredient


class SelectRecipeRequest(BaseModel):
    week_id: str
    week_start: date
    day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    meal_type: str = "dinner"
    recipe_id: str
    target_serves: int | None = None
    include_optional: bool = False


class Timer(BaseModel):
    label: str
    seconds: int
    alertAt: int | None = None
    alertMsg: str | None = None


class Step(BaseModel):
    title: str
    text: str
    timers: list[Timer] = []
    remaining: int | None = None


class SelectRecipeResponse(BaseModel):
    recipe_id: str
    title: str
    brief_description: str
    steps: list[Step]
    ingredients: list[Ingredient]
    image_url: str | None = None
    time: str | None = None
    kcal: int | None = None
    steps_ready: bool = True
    message: str | None = None
