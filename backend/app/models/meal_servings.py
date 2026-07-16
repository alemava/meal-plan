from pydantic import BaseModel, Field


class UpdateMealServingsRequest(BaseModel):
    week_id: str
    meal_id: str
    people: int = Field(gt=0, le=20)


class UpdateMealServingsResponse(BaseModel):
    status: str = "ok"
