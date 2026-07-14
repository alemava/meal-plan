from app.services import history, pool_search
from app.services.generation_rules import INGREDIENT_ITEM_SCHEMA, KCAL_COMPUTATION_RULE
from app.services.profile import UserProfile

# Canonical tool definitions — used by the live tool-use loop (OpenRouter/Groq)
# AND, unchanged, by the standalone MCP server (ADR 5). One set of functions,
# two interfaces.
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_recipe_pool",
            "description": (
                "Search the existing recipe pool for recipes matching a meal type "
                "and cuisine, to check for near-duplicates or draw inspiration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "meal_type": {"type": "string"},
                    "cuisine": {"type": "string"},
                },
                "required": ["meal_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_history",
            "description": "Get recently cooked meal titles and main proteins to avoid repetition.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Lookback window in days"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_recipe",
            "description": "Submit the final generated recipe. Must be called exactly once to finish.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "brief_description": {"type": "string"},
                    "cuisine": {"type": "string"},
                    "main_protein": {"type": "string"},
                    "image_prompt": {"type": "string"},
                    "time": {
                        "type": "string",
                        "description": "Total estimated time, e.g. '25 min'. A real estimate, not a placeholder.",
                    },
                    "kcal": {
                        "type": "integer",
                        "description": KCAL_COMPUTATION_RULE,
                    },
                    "ingredients": {
                        "type": "array",
                        "items": INGREDIENT_ITEM_SCHEMA,
                    },
                },
                "required": [
                    "title",
                    "brief_description",
                    "ingredients",
                    "image_prompt",
                    "time",
                    "kcal",
                ],
            },
        },
    },
]


def build_dispatch(profile: UserProfile) -> dict:
    """Binds tool implementations to this request's user context. The binding
    happens only here, in Python — user_id/profile never appear in anything
    sent to the model (guardrail 19e, payload hygiene)."""

    async def _search_recipe_pool(meal_type: str, cuisine: str | None = None) -> dict:
        result = await pool_search.search_recipe_pool(profile, meal_type)
        if not result:
            return {"found": False}
        return {
            "found": True,
            "title": result["title"],
            "brief_description": result["brief_description"],
            "cuisine": result["cuisine"],
        }

    async def _get_user_history(days: int = 14) -> list[dict]:
        return await history.get_user_history(profile.user_id, days=days)

    return {
        "search_recipe_pool": _search_recipe_pool,
        "get_user_history": _get_user_history,
    }
