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


class RecipeVariation(BaseModel):
    name: str
    add: list[Ingredient] = []
    remove: list[str] = []
    kcal: int | None = None
    time: str | None = None
    # image_prompt is model-authored (only when the variation looks visibly
    # different from the base, e.g. added shrimp) — image_url is filled in
    # lazily, once, the first time a user actually selects that variation
    # (see select_recipe.py), then cached here forever after, same "generate
    # once, reuse forever" pattern as recipes.steps.
    image_prompt: str | None = None
    image_url: str | None = None


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
    variations: list[RecipeVariation] = []
