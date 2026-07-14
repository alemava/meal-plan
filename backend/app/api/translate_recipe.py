from fastapi import APIRouter, Depends, HTTPException

from app.core.security import get_current_user_id
from app.models.translate import TranslateRecipeResponse
from app.services.translate import get_or_create_translation

router = APIRouter(prefix="/api", tags=["recipes"])


@router.get("/translate-recipe", response_model=TranslateRecipeResponse)
async def translate_recipe(
    recipe_id: str, language: str, _user_id: str = Depends(get_current_user_id)
):
    try:
        result = await get_or_create_translation(recipe_id, language)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return TranslateRecipeResponse(**result)
