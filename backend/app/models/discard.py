from typing import Literal

from pydantic import BaseModel


class DiscardRecipeRequest(BaseModel):
    recipe_id: str


class DiscardRecipeResponse(BaseModel):
    status: Literal["ok"] = "ok"
