"""Deterministic safety net for allergen_tags on every newly generated/expanded
recipe. The model already supplies allergen_tags as part of its structured
output (submit_recipe/submit_expansion), but hand-reviewing the 30 seeded
recipes turned up 5 genuinely brand-dependent cases even under careful manual
review — trusting model judgment alone for a safety-critical field isn't
enough. This unions a maintained keyword lookup INTO whatever the model said;
it never removes a tag, only adds ones the model might have missed, biased
toward over-inclusion (a false "contains gluten" is an annoyance, a missed
one is a real safety issue)."""

from app.core import db

ALLERGEN_KEYWORDS: dict[str, list[str]] = {
    "gluten": [
        "pasta",
        "spaghetti",
        "linguine",
        "penne",
        "orecchiette",
        "noodle",
        "bread",
        "breadcrumb",
        "flour",
        "soy sauce",
        "gochujang",
        "doubanjiang",
        "miso",
        "teriyaki",
        "hoisin",
        "beer",
        "barley",
        "wheat",
        "couscous",
        "gnocchi",
        "tortilla",
        "stock",
        "bouillon",
    ],
    "dairy": [
        "milk",
        "cheese",
        "butter",
        "cream",
        "yogurt",
        "yoghurt",
        "parmesan",
        "mozzarella",
        "pecorino",
        "ricotta",
        "mascarpone",
        "ghee",
        "chorizo",
    ],
    "egg": ["egg"],
    "fish": [
        "salmon",
        "cod",
        "tuna",
        "anchovy",
        "sea bass",
        "trout",
        "mackerel",
        "sardine",
        "fish sauce",
        "fish fillet",
        "bonito",
    ],
    "shellfish": [
        "shrimp",
        "prawn",
        "crab",
        "lobster",
        "clam",
        "mussel",
        "scallop",
        "oyster",
        "squid",
        "calamari",
        "curry paste",
    ],
    "peanut": ["peanut"],
    "tree_nut": ["almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut", "macadamia"],
    "soy": [
        "soy sauce",
        "soybean",
        "tofu",
        "miso",
        "edamame",
        "doubanjiang",
        "gochujang",
        "tamari",
    ],
    "sesame": ["sesame", "tahini"],
}

# Substring matching creates real false positives (caught live: "eggplant"
# tripping the "egg" keyword, "coconut milk" — genuinely plant-based, a common
# dairy-free substitute — tripping "milk"). Keyed by the triggering keyword.
KEYWORD_EXCLUSIONS: dict[str, list[str]] = {
    "egg": ["eggplant"],
    "milk": [
        "coconut milk",
        "coconut cream",
        "almond milk",
        "oat milk",
        "soy milk",
        "cashew milk",
        "rice milk",
    ],
}

# Words suggesting a compound/processed ingredient — the category where hidden
# allergens actually live. Deliberately excludes raw produce/protein names
# (potato, carrot, olive oil) so the backlog stays small and high-signal
# rather than drowning in mundane, genuinely-allergen-free ingredients.
COMPOUND_SIGNAL_WORDS = [
    "sauce",
    "paste",
    "stock",
    "broth",
    "powder",
    "seasoning",
    "mix",
    "condiment",
    "dressing",
    "extract",
    "syrup",
    "bouillon",
]


def _looks_compound(name: str) -> bool:
    name = name.lower()
    return any(word in name for word in COMPOUND_SIGNAL_WORDS)


async def log_if_unclassified(ingredient: dict, recipe_id: str) -> None:
    """Gap-visibility mechanism: a keyword list can never be complete. Rather
    than silently trusting a zero-tag result for a compound ingredient the
    safety net doesn't recognise, log it so it becomes a reviewable, growing
    backlog (prioritisable by occurrence count) instead of an invisible gap."""
    name = ingredient.get("name", "")
    if ingredient.get("allergen_tags") or not _looks_compound(name):
        return

    await db.pool().execute(
        """
        INSERT INTO unmapped_ingredients (ingredient_name, example_recipe_id)
        VALUES ($1, $2)
        ON CONFLICT (ingredient_name) DO UPDATE SET
            occurrences = unmapped_ingredients.occurrences + 1,
            last_seen = now()
        """,
        name.strip().lower(),
        recipe_id,
    )


def _keyword_matches(keyword: str, name: str) -> bool:
    if keyword not in name:
        return False
    exclusions = KEYWORD_EXCLUSIONS.get(keyword, [])
    return not any(excl in name for excl in exclusions)


def suggest_allergen_tags(ingredient_name: str) -> list[str]:
    name = ingredient_name.lower()
    return sorted(
        {
            allergen
            for allergen, keywords in ALLERGEN_KEYWORDS.items()
            if any(_keyword_matches(kw, name) for kw in keywords)
        }
    )


def apply_allergen_safety_net(ingredients: list[dict]) -> list[dict]:
    for ingredient in ingredients:
        suggested = suggest_allergen_tags(ingredient.get("name", ""))
        existing = ingredient.get("allergen_tags", [])
        ingredient["allergen_tags"] = sorted(set(existing) | set(suggested))
    return ingredients
