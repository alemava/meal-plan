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
                    "title": {
                        "type": "string",
                        "description": (
                            "Always in English, even if the user's own notes/pantry items were in a "
                            "different language, and even if that's the dish's own name in its native "
                            "language (bad: 'Frutta e Yogurt' for a plain fruit-and-yogurt bowl — "
                            "translate it). The only exception: a single word that's already a "
                            "standard English loanword for an internationally-known dish (e.g. "
                            "'Paella', 'Pad Thai', 'Sushi') — never a whole foreign-language phrase. "
                            "Never include the words 'breakfast', 'lunch', or 'dinner' in the title "
                            "itself (bad: 'Asian-Style Breakfast Fried Noodles') — the same dish can "
                            "genuinely be served at a different meal type later (fried noodles, congee, "
                            "etc. are not breakfast-exclusive), and a meal-type word baked into the name "
                            "reads as wrong/confusing once that happens. Name the dish itself instead."
                        ),
                    },
                    "brief_description": {"type": "string"},
                    "cuisine": {"type": "string"},
                    "main_protein": {"type": "string"},
                    "image_prompt": {
                        "type": "string",
                        "description": (
                            "Describe how the finished plated dish visually looks — colours, "
                            "textures, garnish, vessel, style — never just the dish name (bad: "
                            "'Beef Quesadillas'). If the dish's name is a 'false friend' that "
                            "commonly means a different food in English (e.g. Spanish tortilla is "
                            "an egg-and-potato omelette, not a wrap/flatbread), explicitly rule out "
                            "the wrong reading. End with ', close-up food photography, warm "
                            "natural light, appetizing'."
                        ),
                    },
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
                    "variations": {
                        "type": "array",
                        "description": (
                            "Actively look for natural variants real people actually make — most "
                            "toast/yogurt/egg breakfasts and many mains DO have at least one (different "
                            "nuts/fruit/mix-ins on yogurt, different toast toppings, a genuine "
                            "accompaniment choice for lunch/dinner like a stir-fry or curry served with "
                            "rice vs noodles vs flatbread) — don't skip this by default. 0-3 items, each "
                            "a small DELTA from the base recipe, never a different dish. When the base "
                            "is a main-protein dish traditionally eaten WITH a side but not inherently "
                            "including one (satay, grilled fish/chicken, kebab), offer side/accompaniment "
                            "variations (rice, noodles, flatbread, salad) — those dishes are rarely eaten "
                            "bare. When a well-known variant adds a whole protein/garnish on top of a "
                            "plainer classic dish (e.g. gazpacho with shrimp, carbonara with peas), that "
                            "variant belongs HERE — never as the base recipe itself; the base recipe "
                            "must always be the classic/traditional form. Only omit or use an empty list "
                            "when the recipe truly has no natural variation real people make — never "
                            "invent fake ones just to fill the list, and never force a variation onto a "
                            "dish that doesn't naturally have one."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "e.g. 'With walnuts'"},
                                "add": {"type": "array", "items": INGREDIENT_ITEM_SCHEMA},
                                "remove": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Names of base ingredients this variation replaces/drops.",
                                },
                                "kcal": {
                                    "type": "integer",
                                    "description": (
                                        "TOTAL kcal for the whole recipe when made with this variation "
                                        "instead of the base (same computation as the top-level kcal). "
                                        "Include only when the variation meaningfully changes calories."
                                    ),
                                },
                                "time": {
                                    "type": "string",
                                    "description": (
                                        "TOTAL time for the whole recipe when made with this variation, "
                                        "same format as the top-level time. Include only if the variation "
                                        "genuinely adds/removes real prep or cook time."
                                    ),
                                },
                                "image_prompt": {
                                    "type": "string",
                                    "description": (
                                        "Same rules as the top-level image_prompt (finished-plated-dish "
                                        "description). Include ONLY when this variation would look "
                                        "visibly different in a photo (a whole new visible ingredient) — "
                                        "omit it for changes that wouldn't show, the base recipe's photo "
                                        "is reused for those."
                                    ),
                                },
                            },
                            "required": ["name", "add", "remove"],
                        },
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
