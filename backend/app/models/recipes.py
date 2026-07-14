from typing import Literal

from pydantic import BaseModel

ScalingCategory = Literal["linear", "seasoning", "heat", "fixed"]
IngredientTier = Literal["mandatory", "recommended", "optional"]


class Ingredient(BaseModel):
    name: str
    qty: float
    unit: str
    scaling: ScalingCategory
    tier: IngredientTier
    allergen_tags: list[str] = []


class RecipeOption(BaseModel):
    id: str
    title: str
    brief_description: str
    cuisine: str | None = None
    main_protein: str | None = None
    ingredients: list[Ingredient]
    image_url: str | None = None
    status: Literal["stub", "partial", "complete"]
    source: str
    base_serves: int | None = None
    time: str | None = None
    kcal: int | None = None
