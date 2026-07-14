"""Shared generation rules (2026-07-14) — single source for instructions that
were previously duplicated, and prone to silent drift, across tools.py,
stub_expansion.py, fresh_generation.py and pool_warmer.py. The dish-
authenticity rule sat unfixed in pool_warmer for weeks after being added to
fresh_generation precisely because nothing forced the two copies to stay in
sync. Every prompt-building site should compose from here instead of writing
its own copy."""

INGREDIENT_ITEM_SCHEMA = {
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
}

# Core authenticity rule. fresh_generation.py appends its own extra sentence
# about not forcing an unrealistic protein swap into a classic dish (specific
# to its protein-avoidance feature, not a general authoring rule) after this.
DISH_AUTHENTICITY_RULE = (
    "The dish comes first, not the ingredients: every recipe must be a REAL, recognizable "
    "dish that genuinely exists in that cuisine's tradition — never invent an unusual "
    "ingredient combination or a generic 'fusion'/'twist'-style mashup just to sound "
    "appealing or to satisfy variety."
)

# New (2026-07-14) — preventive half of the defining-ingredient gap found by
# the content-quality audit (Pad Thai with no tamarind/fish sauce/palm sugar,
# Shawarma/Doner with no meat-seasoning spices). recipe_audit.py's semantic
# check is the detective half; this is the half that tries to stop it at
# generation time in the first place.
DEFINING_COMPONENTS_RULE = (
    "Include every component that defines the dish by name — its sauce (e.g. pad thai's "
    "tamarind-fish sauce-palm sugar base), its spice blend (e.g. shawarma's cumin/paprika/"
    "turmeric), the cheese it's named after, etc. A dish missing its defining component is "
    "wrong even if everything else listed is individually sensible."
)

KCAL_COMPUTATION_RULE = (
    "Total kcal for the WHOLE recipe as written, computed ingredient-by-ingredient from "
    "standard nutrition values (USDA or regional equivalent) at the exact qty/unit given — "
    "never an impression-based guess. Don't forget oils/fats (count every tablespoon) and "
    "dry-vs-cooked weight for pasta/rice/grains."
)
