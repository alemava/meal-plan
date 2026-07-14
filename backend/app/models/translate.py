from pydantic import BaseModel

from app.models.recipes import Ingredient
from app.models.select import Step


class TranslateRecipeResponse(BaseModel):
    title: str
    ingredients: list[Ingredient]
    steps: list[Step]
