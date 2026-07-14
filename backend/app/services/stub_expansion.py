from app.core import db
from app.services import ai_client, allergens
from app.services.guardrails import GeneratedRecipeInvalid, validate_ingredient_shape

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
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "qty": {"type": "number"},
                                "unit": {
                                    "type": "string",
                                    "description": (
                                        "Metric only — g, kg, ml, l, tsp, tbsp, or a plain count "
                                        "(whole, clove, slice, piece, sheet, fillet, leaf, etc). "
                                        "Never imperial (no lb, oz, pound, inch, cup as a weight "
                                        "substitute) — a real, recurring failure mode where quantities "
                                        "silently fail to sum correctly on the shopping list."
                                    ),
                                },
                                "scaling": {
                                    "type": "string",
                                    "enum": ["linear", "seasoning", "heat", "fixed"],
                                },
                                "tier": {
                                    "type": "string",
                                    "enum": ["mandatory", "recommended", "optional"],
                                },
                                "allergen_tags": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["name", "qty", "unit", "scaling", "tier"],
                        },
                    },
                    "time": {
                        "type": "string",
                        "description": "Total estimated time, e.g. '25 min'. A real estimate, not a placeholder.",
                    },
                    "kcal": {
                        "type": "integer",
                        "description": (
                            "Total kcal for the WHOLE recipe as written, computed "
                            "ingredient-by-ingredient from standard nutrition values (USDA or "
                            "regional equivalent) at the exact qty/unit given — never an "
                            "impression-based guess. Don't forget oils/fats and dry-vs-cooked "
                            "weight for pasta/rice."
                        ),
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
        "title and description, generate a realistic, well-balanced ingredient list."
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
    for _ in range(MAX_EXPANSION_ATTEMPTS):
        try:
            result = await ai_client.run_tool_use_loop(
                system_prompt,
                user_prompt,
                EXPANSION_TOOLS,
                dispatch={},
                purpose="stub_expansion",
                final_tool_name="submit_expansion",
                recipe_id=str(stub["id"]),
            )
            validate_ingredient_shape(result["ingredients"])
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
            status = 'partial', source = 'openrouter'
        WHERE id = $5
        """,
        ingredients,
        required_tags,
        result.get("time"),
        result.get("kcal"),
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
