from app.core import db
from app.core.config import get_settings
from app.services import ai_client
from app.services.steps_generation import sanitize_step_text

TRANSLATE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "submit_translation",
            "description": "Submit the translated recipe content. Call exactly once to finish.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "ingredient_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Translated ingredient names, same order as given",
                    },
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "text": {
                                    "type": "string",
                                    "description": "Keep any <strong>...</strong> tags around ingredient names, translated in place.",
                                },
                            },
                            "required": ["title", "text"],
                        },
                    },
                },
                "required": ["title", "ingredient_names", "steps"],
            },
        },
    }
]


async def get_or_create_translation(recipe_id: str, language: str) -> dict:
    """Step 28/29 — recipe_translations is a read-time cache; the source
    recipe and its embedding are never touched by this."""
    cached = await db.pool().fetchrow(
        "SELECT title, ingredients, steps FROM recipe_translations WHERE recipe_id = $1 AND language = $2",
        recipe_id,
        language,
    )
    if cached:
        return dict(cached)

    recipe = await db.pool().fetchrow(
        "SELECT title, ingredients, steps FROM recipes WHERE id = $1", recipe_id
    )
    if recipe is None:
        raise ValueError("Recipe not found")

    translated = await _translate(recipe, language, recipe_id)

    await db.pool().execute(
        """
        INSERT INTO recipe_translations (recipe_id, language, title, ingredients, steps, translated_by)
        VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6)
        ON CONFLICT (recipe_id, language) DO NOTHING
        """,
        recipe_id,
        language,
        translated["title"],
        translated["ingredients"],
        translated["steps"],
        f"ai:{get_settings().ai_provider}",
    )
    return translated


async def _translate(recipe: dict, language: str, recipe_id: str) -> dict:
    ingredients = recipe["ingredients"] or []
    steps = recipe["steps"] or []
    ingredient_names = [i["name"] for i in ingredients]

    system_prompt = (
        f"You are translating a recipe into {language}. Keep the meaning precise — "
        "this is used for cooking instructions, not marketing copy."
    )
    user_prompt = (
        f"Title: {recipe['title']}\n"
        f"Ingredient names: {ingredient_names}\n"
        f"Steps: {[(s['title'], s['text']) for s in steps]}\n"
        f"Translate the title, ingredient names (same order), and steps into {language}."
    )

    result, _provider = await ai_client.run_tool_use_loop(
        system_prompt,
        user_prompt,
        TRANSLATE_TOOLS,
        dispatch={},
        purpose="translate_recipe",
        final_tool_name="submit_translation",
        recipe_id=recipe_id,
    )

    translated_ingredients = [
        {**ingredient, "name": name}
        for ingredient, name in zip(ingredients, result["ingredient_names"], strict=True)
    ]

    # timers/remaining are numeric/structural, not language-dependent — carry
    # them over from the source step rather than trusting the model to echo
    # them back unchanged. Text is sanitized the same way as fresh generation
    # (only <strong> survives) since this is model output too.
    translated_steps = [
        {
            "title": translated["title"],
            "text": sanitize_step_text(translated.get("text") or ""),
            "timers": source.get("timers") or [],
            "remaining": source.get("remaining"),
        }
        for source, translated in zip(steps, result["steps"], strict=True)
    ]

    return {
        "title": result["title"],
        "ingredients": translated_ingredients,
        "steps": translated_steps,
    }
