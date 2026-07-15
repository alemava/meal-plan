"""Realistic mesa slot-request fixtures for the DeepInfra model benchmark.

Shaped exactly like a real UserProfile + slot request (app.services.profile,
app.models.generate.SlotRequest) — never a bare "write a recipe" prompt —
because that's the actual unit of work fresh_generation.py handles in
production. cuisines/allergies/dislikes/comment/max_time_minutes are the
only levers a real request has; variety here comes from combining them the
way a real user profile + slot would, not from inventing new fields.
"""

from dataclasses import dataclass


@dataclass
class Case:
    label: str
    meal_type: str  # breakfast | lunch | dinner | special
    cuisines: list[str]
    allergies: list[str]
    dislikes: list[str]
    comment: str | None
    max_time_minutes: int | None


# 24 cases. Each hits a distinct, realistic combination — covers every
# category the brief asked for (breakfast/lunch/dinner/dessert, vegetarian/
# vegan/gluten-free/dairy-free, quick/cheap/high-protein/kid-friendly,
# mediterranean/mexican/asian/simple-ingredient) without a combinatorial
# explosion of the full cross-product.
CASES: list[Case] = [
    Case("breakfast_mediterranean", "breakfast", ["mediterranean"], [], [], None, None),
    Case("breakfast_quick", "breakfast", ["italian"], [], [], "quick, minimal ingredients", 10),
    Case("breakfast_highprotein", "breakfast", ["american"], [], [], "high protein", 20),
    Case("breakfast_vegan", "breakfast", ["mexican"], ["dairy", "egg"], ["meat"], "vegan", 20),
    Case("lunch_glutenfree", "lunch", ["italian"], ["gluten"], [], None, 30),
    Case("lunch_mexican_budget", "lunch", ["mexican"], [], [], "budget-friendly, cheap ingredients", 25),
    Case("lunch_asian_quick", "lunch", ["asian"], [], [], "quick weekday lunch", 20),
    Case("lunch_vegetarian", "lunch", ["indian"], [], ["chicken", "beef", "pork"], "vegetarian", 35),
    Case("lunch_kidfriendly", "lunch", ["italian"], [], [], "kid-friendly, mild flavors", 25),
    Case("lunch_dairyfree", "lunch", ["mediterranean"], ["dairy"], [], None, 30),
    Case("dinner_mediterranean", "dinner", ["mediterranean"], [], [], None, 45),
    Case("dinner_mexican", "dinner", ["mexican"], [], [], None, 40),
    Case("dinner_asian_simple", "dinner", ["asian"], [], [], "simple, few ingredients", 30),
    Case("dinner_spanish", "dinner", ["spanish"], [], [], None, 45),
    Case("dinner_indian", "dinner", ["indian"], [], [], None, 45),
    Case("dinner_highprotein_glutenfree", "dinner", ["american"], ["gluten"], [], "high protein", 35),
    Case("dinner_vegan_budget", "dinner", ["mexican"], ["dairy", "egg"], ["meat"], "vegan, budget-friendly", 30),
    Case("dinner_quick_weeknight", "dinner", ["italian"], [], [], "quick weeknight dinner, minimal cleanup", 20),
    Case("dinner_kidfriendly_dairyfree", "dinner", ["american"], ["dairy"], [], "kid-friendly", 30),
    Case("dinner_glutenfree_asian", "dinner", ["asian"], ["gluten"], [], None, 35),
    Case("special_dessert_mediterranean", "special", ["mediterranean"], [], [], "dessert", 30),
    Case("special_dessert_vegan", "special", ["american"], ["dairy", "egg"], [], "vegan dessert", 30),
    Case("special_snack_highprotein", "special", ["american"], [], [], "high-protein snack", 15),
    Case("special_appetizer_spanish", "special", ["spanish"], [], [], "appetizer for guests", 25),
]

# Small subset reused for the concurrency-scaling round (R4) and the
# multi-recipe-per-prompt round (R3-lite) — deliberately a fixed, varied
# slice of CASES above rather than a separate hand-written set, so results
# stay comparable to the R1 core-matrix numbers for the same exact prompts.
CONCURRENCY_SUBSET = [c for c in CASES if c.label in {
    "breakfast_quick", "lunch_mexican_budget", "dinner_mediterranean",
    "dinner_vegan_budget", "dinner_quick_weeknight", "special_dessert_mediterranean",
}]

# Steps-generation round (RS) reuses a subset too, but that stage needs a
# already-generated recipe (title/ingredients) as input, not a fresh slot
# request — built from CONCURRENCY_SUBSET's cases at run time once R1's
# real generated ingredients are available (see runner.py).
STEPS_SUBSET_LABELS = {c.label for c in CONCURRENCY_SUBSET}
