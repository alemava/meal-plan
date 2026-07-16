from datetime import date, timedelta

from app.models.generate import PantryItem
from app.services.profile import UserProfile

# Non-food / dangerous substances the model must never include, regardless of
# where the text came from (there's no live free-text ingredient input today,
# but this protects against prompt injection or plain hallucination too — the
# same "don't trust the model" philosophy as the allergen safety net below).
# Substring match, case-insensitive — tunable, not exhaustive.
DANGEROUS_TERMS = [
    "poison",
    "veneno",
    "bleach",
    "lejía",
    "lejia",
    "ammonia",
    "amoniaco",
    "antifreeze",
    "anticongelante",
    "rat poison",
    "matarratas",
    "cyanide",
    "cianuro",
    "arsenic",
    "arsénico",
    "arsenico",
    "detergent",
    "detergente",
    "feces",
    "faeces",
    "mierda",
    "excremento",
    "urine",
    "orina",
    "meado",
    "pufferfish",
    "fugu",
    "pez globo",
]


class GeneratedRecipeInvalid(Exception):
    """Raised by validate_generated_recipe — the caller's job is to reject and
    regenerate (step 19a), not to patch the recipe up."""


# Canonical unit whitelist (2026-07-14) — same set as the content-quality
# audit's scan script (final_scan.py's KNOWN_UNITS), promoted from a
# scratchpad diagnostic into a permanent at-source guardrail. Neither
# submit_recipe's nor submit_expansion's schema ever told the model units
# must be metric until generation_rules.INGREDIENT_ITEM_SCHEMA added a
# description — this is the deterministic backstop for when the model
# ignores that description anyway (proven necessary: 15 imperial-unit
# violations were produced by this session's own live testing, after the
# historical unit sweep had already run and closed).
ALLOWED_UNITS: frozenset[str] = frozenset(
    {
        "g", "kg", "ml", "l", "tsp", "tbsp", "tbs", "cup", "cups",
        "whole", "clove", "cloves", "piece", "pieces", "slice", "slices",
        "can", "bunch", "handful", "head", "leaf", "leaves", "pinch",
        "wedge", "wedges", "sprig", "sprigs", "stalk", "stalks",
        "grams", "milliliters", "teaspoon", "teaspoons", "tablespoon", "tablespoons",
        "medium", "large", "small", "fillet", "fillets", "portion",
        "sheet", "sheets", "tin", "pack", "to taste",
    }
)


def validate_ingredient_units(ingredients: list) -> None:
    """Reject-and-regenerate counterpart to ALLOWED_UNITS. Same lenient
    first-word match as the scan script (tolerates a parenthetical
    clarification like 'cup (240ml)') so this doesn't flag formats the scan
    itself considered fine — only a genuinely unrecognised/imperial/missing
    unit trips it."""
    for ingredient in ingredients:
        unit = (ingredient.get("unit") or "").lower().strip()
        if not unit:
            raise GeneratedRecipeInvalid(f"Ingredient '{ingredient.get('name')}' has no unit")
        head = unit.split("(")[0].strip()
        first_word = head.split()[0] if head else ""
        if unit not in ALLOWED_UNITS and first_word not in ALLOWED_UNITS:
            raise GeneratedRecipeInvalid(
                f"Ingredient '{ingredient.get('name')}' has a disallowed unit: "
                f"'{ingredient.get('unit')}'"
            )


def validate_ingredient_shape(ingredients: list) -> None:
    """Runs before anything else touches the model's output (allergen safety
    net, then validate_generated_recipe below) — both assume every element is
    a dict via .get()/[...], and a malformed tool-call under load has been
    seen live returning a bare ingredient name string instead of an object,
    crashing with an unhandled AttributeError deep in a keyword-matching
    helper. Same "reject and regenerate" contract as GeneratedRecipeInvalid
    below, just catching a shape problem instead of a content problem."""
    for ingredient in ingredients:
        if not isinstance(ingredient, dict):
            raise GeneratedRecipeInvalid(
                f"Malformed ingredient (expected object, got {type(ingredient).__name__}): "
                f"{ingredient!r}"
            )


def validate_generated_recipe(recipe: dict, profile: UserProfile, recent_titles: list[str]) -> None:
    """Step 19(a) — hard validation the model cannot be trusted to enforce on
    its own: allergen exclusions, no-repeat windows, basic portion sanity.
    18b already asks the model to avoid these; this is the safety net for
    when the model ignores that instruction, not the primary mechanism."""
    ingredients = recipe.get("ingredients") or []
    validate_ingredient_units(ingredients)

    searchable_text = " ".join(
        [recipe.get("title") or "", recipe.get("brief_description") or ""]
        + [i.get("name") or "" for i in ingredients]
    ).lower()
    for term in DANGEROUS_TERMS:
        if term in searchable_text:
            raise GeneratedRecipeInvalid(f"Recipe contains a disallowed term: '{term}'")

    for ingredient in ingredients:
        if set(ingredient.get("allergen_tags", [])) & set(profile.allergies):
            raise GeneratedRecipeInvalid(
                f"Ingredient '{ingredient.get('name')}' conflicts with an allergy"
            )
        qty = ingredient.get("qty", 0)
        # A model returning qty as a numeric-looking string (e.g. "200"
        # instead of 200) used to crash this with an uncaught TypeError —
        # found live via the DeepInfra benchmark — instead of the intended
        # reject-and-regenerate path every other bad-content case here gets.
        if not isinstance(qty, int | float) or isinstance(qty, bool) or qty <= 0:
            raise GeneratedRecipeInvalid(f"Ingredient '{ingredient.get('name')}' has invalid qty")

    if recipe.get("title") in recent_titles:
        raise GeneratedRecipeInvalid(f"Title '{recipe.get('title')}' repeats a recent meal")


def sanitize_user_comment(comment: str | None) -> str | None:
    """The first live free-text input in mesa's generation pipeline (a
    per-slot craving/note) — checked against the same DANGEROUS_TERMS list
    used on generated output, before it ever reaches a prompt. A flagged
    comment is silently discarded (the recipe still generates normally,
    just without honoring that craving) rather than failing the whole
    request over one bad comment."""
    if not comment or not comment.strip():
        return None
    lowered = comment.lower()
    for term in DANGEROUS_TERMS:
        if term in lowered:
            return None
    return comment.strip()


def sanitize_pantry_ingredients(pantry: list[PantryItem] | None) -> list[dict]:
    """Per-item guardrail for the user's declared on-hand ingredients (Q1) —
    unlike sanitize_user_comment (an optional craving, fine to drop wholesale
    on any red flag), this is a positive-constraint list the model is meant
    to actually honor: one bad line shouldn't cost the user their whole
    pantry, so only the flagged item is dropped, never the entire list. Same
    DANGEROUS_TERMS backstop already used on generated output and on the
    per-slot comment; no exhaustive "is this really food" classifier, same
    "skipped honestly rather than faked" precedent as pool_search.py's
    documented pantry-matching gap.
    """
    if not pantry:
        return []
    sanitized = []
    for item in pantry:
        name = (item.name or "").strip()
        if not name:
            continue
        lowered = name.lower()
        if any(term in lowered for term in DANGEROUS_TERMS):
            continue
        sanitized.append({"name": name, "qty": item.qty, "unit": item.unit})
    return sanitized


def normalise_to_monday(requested: date) -> date:
    """Guardrail 19(d): never trust the caller's week_start.

    The frontend does exact string equality against thisWeekMonday() to pick
    the active week — a non-Monday date silently breaks that, so every write
    path must go through this first.
    """
    return requested - timedelta(days=requested.weekday())


def week_id_for(monday: date) -> str:
    return f"{monday.isoformat()}-ai"
