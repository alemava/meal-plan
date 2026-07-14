from app.models.recipes import Ingredient

# Phase 3 research: tunable defaults, not ground truth. See HANDOVER.md item 9.
SCALING_EXPONENTS = {
    "linear": 1.0,
    "seasoning": 0.8,
    "heat": 0.65,
    "fixed": 0.0,  # exponent unused — fixed ingredients never scale, handled explicitly below
}


def scale_ingredient(ingredient: Ingredient, ratio: float) -> Ingredient:
    if ingredient.scaling == "fixed":
        scaled_qty = ingredient.qty
    else:
        exponent = SCALING_EXPONENTS[ingredient.scaling]
        scaled_qty = ingredient.qty * (ratio**exponent)

    return ingredient.model_copy(update={"qty": round(scaled_qty, 2)})


def scale_recipe_ingredients(
    ingredients: list[Ingredient],
    base_serves: int,
    target_serves: int,
    include_recommended: bool = True,
    include_optional: bool = False,
) -> list[Ingredient]:
    ratio = target_serves / base_serves

    selected = [
        i
        for i in ingredients
        if i.tier == "mandatory"
        or (i.tier == "recommended" and include_recommended)
        or (i.tier == "optional" and include_optional)
    ]
    return [scale_ingredient(i, ratio) for i in selected]
