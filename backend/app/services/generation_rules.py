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
        "name": {
            "type": "string",
            "description": (
                "In English, even if the user's own notes/pantry items named it in a different "
                "language (e.g. a pantry item typed as 'pollo' must appear here as 'chicken')."
            ),
        },
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
    "appealing or to satisfy variety. This applies just as strongly when trying to honor "
    "several hints at once (a craving, pantry items on hand, a cuisine preference, etc.) — "
    "real bug found live: 'pasta' craving + mango/tabasco on hand produced an invented "
    "'Pasta con Pollo alla Mango e Tabasco', not a real dish. If satisfying every hint "
    "together would require inventing something, satisfy FEWER of them with a real dish "
    "instead. A real, authentic dish that ignores an optional hint is always correct; an "
    "invented dish that satisfies every hint is always wrong. "
    "When a dish has both a classic/traditional form and a well-known variant with an "
    "added protein or garnish (e.g. gazpacho vs. gazpacho with shrimp; carbonara vs. "
    "carbonara with peas), the base recipe must be the classic form — bake the well-known "
    "variant into 'variations' instead of making it the main suggestion (real bug found "
    "live: 'Spanish Gazpacho with Grilled Shrimp' was generated as the base recipe, with "
    "shrimp as a MANDATORY ingredient, when plain gazpacho is the actual classic dish and "
    "shrimp is only ever an add-on some people make)."
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

# 2026-07-18 — real bug found live: a user typed a pantry item in Spanish
# ("pollo") to test whether the model understood it; it did, but then wrote
# "pollo" into the recipe's own title AND ingredient list instead of
# translating it — mesa's UI/database are English-only until a real i18n
# phase exists, so any foreign-language text in a comment/pantry item must
# never leak into stored output.
#
# 2026-07-19 — narrowed further, real complaint from live testing: the
# original version let "an authentic dish's own proper name stay in its
# native language", intended for genuinely internationally-known names like
# "Pad Thai" — but the model used it as license to leave plain, ordinary
# foreign phrases untranslated too (e.g. "Frutta e Yogurt", literally just
# Italian for "fruit and yogurt", not a special dish name at all). The title
# must now ALWAYS be in the interface language (English here; Spanish once
# that UI ships) — the only thing that may stay foreign is a single already-
# English-loanword dish name (paella, risotto, sushi, taco, quesadilla —
# words English speakers already use as-is), never a whole untranslated
# phrase.
ENGLISH_OUTPUT_RULE = (
    "Every output field (title, brief_description, ingredient names, steps) must be "
    "written in English, even if the user's own notes or pantry items are written in a "
    "different language — translate any foreign-language ingredient the user mentioned "
    "into its English name in the output. The TITLE must be in English too, with no "
    "exception for 'this is the dish's real name' — translate/describe it in English "
    "instead (bad: 'Frutta e Yogurt', 'Chocolate con Churros' — these are just literal "
    "foreign words for 'Fruit and Yogurt', 'Chocolate with Churros', not special names; "
    "good: 'Fruit and Yogurt Bowl', 'Churros with Chocolate Sauce'). The only thing "
    "allowed to stay non-English is a single word that's already a standard English "
    "loanword for an internationally-known dish (e.g. 'Paella', 'Risotto', 'Pad Thai', "
    "'Sushi', 'Quesadilla') — never a whole foreign-language phrase or sentence."
)

# 2026-07-18 — real bug found live: a plain breakfast request came back with
# "Huevos Rotos", a real Spanish dish but a lunch/tapas one (fried eggs over
# potatoes, often chorizo/ham) — never eaten for breakfast in Spain. Root
# cause: meal_type reached the prompt as a bare label ("Generate a breakfast
# recipe...") with no rule that the dish must genuinely BE a breakfast dish.
# Deliberately does NOT hardcode "cuisine X eats Y for breakfast" — that's a
# real stereotyping risk; this constrains STRUCTURE (must be a real
# breakfast dish) and leaves WHICH dish to the model's own knowledge of the
# preferred cuisine. Only appended to the prompt when meal_type=="breakfast".
BREAKFAST_AUTHENTICITY_RULE = (
    "This is a BREAKFAST recipe — it must be a dish genuinely eaten for breakfast/morning "
    "in its cuisine's real tradition, not a lunch or dinner dish that merely happens to fit "
    "the ingredients (real bug found live: 'Huevos Rotos' is a real Spanish dish, but a "
    "lunch/tapas one, never breakfast). If the preferred cuisine doesn't have a strong, "
    "distinct breakfast tradition of its own, prefer a broadly-recognized international "
    "breakfast style (eggs, toast, oats, pancakes, yogurt/granola, etc.) over forcing an "
    "inauthentic '[cuisine] breakfast.' "
    # 2026-07-19 — when a profile lists several cuisines that span very
    # different breakfast cultures (e.g. Italian/Spanish + Asian), each
    # individual suggestion was authentic (congee IS a real Asian breakfast)
    # but surprised the user, since no cuisine here is their actual home
    # culture — there is no country-of-residence signal to say so (see
    # HANDOVER.md). This is a DEFAULT to prefer, not a ban: the user's own
    # note can still ask for it explicitly.
    "When the user's preferred cuisines span multiple, very different breakfast "
    "traditions, default to a broadly-recognized/European-style breakfast rather than one "
    "highly specific to a single distant tradition (e.g. congee, natto, century eggs) — "
    "unless the user's own note explicitly asks for that cuisine's breakfast."
)


def seasonal_instruction() -> str:
    """2026-07-19, user-requested: fresher/lighter dishes in summer, warming/
    heartier ones in winter. Northern-hemisphere default — same gap as
    BREAKFAST_AUTHENTICITY_RULE's multi-culture case: no country-of-
    residence signal exists yet (see HANDOVER.md), so this can't know a
    Southern-Hemisphere user's real season. A soft bias, never a hard
    restriction — the user's own craving note can always ask for a hearty
    stew in July or a cold salad in January."""
    import datetime

    month = datetime.datetime.now(datetime.UTC).month
    if month in (6, 7, 8):
        season, guidance = "summer", "lighter, fresher dishes — salads, grilled proteins, cold soups, chilled desserts"
    elif month in (12, 1, 2):
        season, guidance = "winter", "warming, heartier dishes — stews, roasts, braises, soups"
    elif month in (3, 4, 5):
        season, guidance = "spring", "fresh, lighter dishes built around early-season produce"
    else:
        season, guidance = "autumn", "warming, comforting dishes with seasonal root vegetables and squash"
    return (
        f"It's currently {season} (Northern Hemisphere default — no home-region signal exists yet). "
        f"Where it genuinely fits the requested cuisine and meal type, lean toward {guidance}. This is "
        "a soft preference, not a restriction — never override real dish authenticity for it, and the "
        "user's own craving note can always ask for something else regardless of season."
    )
