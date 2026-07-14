from datetime import date, timedelta

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
        if ingredient.get("qty", 0) <= 0:
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


def normalise_to_monday(requested: date) -> date:
    """Guardrail 19(d): never trust the caller's week_start.

    The frontend does exact string equality against thisWeekMonday() to pick
    the active week — a non-Monday date silently breaks that, so every write
    path must go through this first.
    """
    return requested - timedelta(days=requested.weekday())


def week_id_for(monday: date) -> str:
    return f"{monday.isoformat()}-ai"
