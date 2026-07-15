from app.core import db
from app.services import ai_client, allergens
from app.services.generation_rules import (
    DEFINING_COMPONENTS_RULE,
    INGREDIENT_ITEM_SCHEMA,
    KCAL_COMPUTATION_RULE,
)
from app.services.guardrails import (
    GeneratedRecipeInvalid,
    validate_ingredient_shape,
    validate_ingredient_units,
)

# Mirrors fresh_generation.py's MAX_GENERATION_ATTEMPTS — same underlying
# tool-call flakiness risk (a malformed response under load), same "fresh
# conversation, don't waste the other retries" fix.
MAX_EXPANSION_ATTEMPTS = 3

EXPANSION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "submit_expansion",
            "description": "Submit the ingredients for this recipe stub. Call exactly once to finish.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ingredients": {
                        "type": "array",
                        "items": INGREDIENT_ITEM_SCHEMA,
                    },
                    "time": {
                        "type": "string",
                        "description": "Total estimated time, e.g. '25 min'. A real estimate, not a placeholder.",
                    },
                    "kcal": {
                        "type": "integer",
                        "description": KCAL_COMPUTATION_RULE,
                    },
                },
                "required": ["ingredients", "time", "kcal"],
            },
        },
    },
]


async def expand_stub(stub: dict) -> dict:
    """Step 16 — a stub surfaced by the hybrid search has an embedding and
    image already (from pool_warmer) but no ingredients. Expanding it here
    defers ALL generation cost until a real user's search actually needs it."""
    system_prompt = (
        "You are completing a recipe stub for a meal-planning app. Given the "
        "title and description, generate a realistic, well-balanced ingredient list. "
        f"{DEFINING_COMPONENTS_RULE}"
    )
    user_prompt = (
        f"Title: {stub['title']}\n"
        f"Description: {stub['brief_description']}\n"
        f"Cuisine: {stub.get('cuisine') or 'unspecified'}\n"
        f"Main protein: {stub.get('main_protein') or 'unspecified'}\n"
        "Generate the ingredients for this recipe."
    )

    last_error: Exception | None = None
    result = None
    provider = "openrouter"
    for _ in range(MAX_EXPANSION_ATTEMPTS):
        try:
            result, provider = await ai_client.run_tool_use_loop(
                system_prompt,
                user_prompt,
                EXPANSION_TOOLS,
                dispatch={},
                purpose="stub_expansion",
                final_tool_name="submit_expansion",
                recipe_id=str(stub["id"]),
            )
            validate_ingredient_shape(result["ingredients"])
            validate_ingredient_units(result["ingredients"])
            break
        except (GeneratedRecipeInvalid, ai_client.AIProviderExhausted) as exc:
            last_error = exc
            result = None
            continue
    if result is None:
        raise ai_client.AIProviderExhausted from last_error

    ingredients = allergens.apply_allergen_safety_net(result["ingredients"])
    required_tags = sorted({i["name"].lower().replace(" ", "_") for i in ingredients})

    await db.pool().execute(
        """
        UPDATE recipes
        SET ingredients = $1::jsonb, required_ingredient_tags = $2, time = $3, kcal = $4,
            status = 'partial', source = $5
        WHERE id = $6
        """,
        ingredients,
        required_tags,
        result.get("time"),
        result.get("kcal"),
        provider,
        stub["id"],
    )

    for ingredient in ingredients:
        await allergens.log_if_unclassified(ingredient, str(stub["id"]))

    expanded = dict(stub)
    expanded["ingredients"] = ingredients
    expanded["status"] = "partial"
    expanded["time"] = result.get("time")
    expanded["kcal"] = result.get("kcal")
    return expanded
