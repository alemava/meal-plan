"""Part C (2026-07-14) — periodic re-audit of the recipe pool. Closes the 4th
gap the content-quality audit found: a one-time sweep doesn't stay clean,
since it only fixes existing rows, never the generation prompt that produced
them (proven twice this session: the allergen backfill missed a later
source, and 15 new imperial-unit violations appeared from this session's own
live testing after the historical unit sweep had already closed).

Same design principle as allergens.py: deterministic issues (unit/shape/step
format) are already blocked+retried at generation time by guardrails.py and
steps_generation.py (Part B) — this module never re-decides those, it just
re-scans for anything that slipped through some other path (a historical
row, a direct DB edit, a future code path that forgets to call the
guardrail). Judgment-based issues (kcal plausibility, whether a dish has its
defining ingredients) are flag-and-report ONLY, same as unmapped_ingredients
— never silently auto-fixed.

Triggered at the end of run_pool_warmer() (exactly when new content is
created, no extra Cloud Scheduler job) and via POST /api/admin/recipe-audit
on demand.
"""

import json
import re
from datetime import UTC, datetime

from pydantic import ValidationError

from app.core import db
from app.models.recipes import Ingredient
from app.services import cloudflare, provider_quality, resend_client
from app.services.guardrails import GeneratedRecipeInvalid, validate_ingredient_units
from app.services.steps_generation import GeneratedStepsInvalid, validate_steps

SEMANTIC_CHECK_NAME = "missing_defining_ingredient"

# Flag-only thresholds (Fix 1) — deliberately loose. This is a gross-error
# catcher (it would have caught churros: stored 1200 vs a ~4300 rough
# estimate), not a precision nutrition tool. Ingredients with a lot of
# unmatched mass (generic "vegetable"-bucket guesses) get a wider tolerance
# so vague-but-honest ingredient lists don't drown out real errors.
KCAL_RATIO_THRESHOLD = 2.5
KCAL_RATIO_THRESHOLD_LOOSE = 4.0
UNMATCHED_MASS_TOLERANCE = 0.3
MIN_PLAUSIBLE_KCAL_PER_SERVING = 120
MAX_PLAUSIBLE_KCAL_PER_SERVING = 2600

# kcal per gram, keyword-matched against the ingredient name (first match
# wins — order matters, more specific keywords first). Rough, USDA-ballpark
# figures, not looked up per-ingredient.
KCAL_PER_GRAM: dict[str, float] = {
    "olive oil": 8.8, "vegetable oil": 8.8, "sesame oil": 8.8, "oil": 8.8,
    "ghee": 8.8, "lard": 9.0, "butter": 7.2, "mayonnaise": 6.8,
    "chocolate": 5.4, "cocoa": 4.8, "bacon": 5.4, "tahini": 5.9,
    "nut": 6.0, "almond": 5.8, "peanut": 5.7, "sesame": 5.7, "cashew": 5.5, "pistachio": 5.6,
    "cheese": 3.8, "cream": 3.4, "coconut milk": 2.0, "coconut cream": 3.3,
    "sugar": 4.0, "honey": 3.0, "syrup": 2.9, "jam": 2.6,
    "sausage": 3.0, "chorizo": 3.0, "salami": 3.8, "pepperoni": 5.0,
    "beef": 2.5, "lamb": 2.5, "pork": 2.4, "duck": 3.4,
    "chicken thigh": 2.1, "chicken": 1.9, "turkey": 1.9,
    "salmon": 2.1, "tuna": 1.3, "fish": 1.5, "cod": 0.8, "anchovy": 2.1,
    "shrimp": 1.0, "prawn": 1.0, "crab": 0.9, "lobster": 0.9, "squid": 0.9, "mussel": 0.9,
    "tofu": 0.76, "tempeh": 1.9, "seitan": 1.7,
    "flour": 3.6, "breadcrumb": 3.8, "bread": 2.65, "tortilla": 3.0,
    "pasta": 3.5, "spaghetti": 3.5, "noodle": 3.5, "rice": 3.5, "oats": 3.9, "couscous": 3.5,
    "avocado": 1.6, "olive": 1.5, "potato": 0.77, "sweet potato": 0.86,
    "banana": 0.9, "dried fruit": 3.0, "fruit": 0.5,
    "onion": 0.4, "garlic": 1.5, "carrot": 0.41, "tomato": 0.2, "pepper": 0.31,
    "mushroom": 0.22, "vegetable": 0.35, "leafy green": 0.25, "spinach": 0.23, "lettuce": 0.15,
    "egg": 1.4, "milk": 0.62, "yogurt": 0.6, "yoghurt": 0.6,
    "stock": 0.05, "broth": 0.05, "wine": 0.83, "beer": 0.43, "vinegar": 0.2,
}
DEFAULT_KCAL_PER_GRAM = 1.5

# Unit -> grams, for units that mean a fixed-ish mass regardless of what
# they're attached to.
GRAMS_PER_UNIT: dict[str, float] = {
    "tsp": 5, "teaspoon": 5, "teaspoons": 5,
    "tbsp": 15, "tablespoon": 15, "tablespoons": 15,
    "clove": 5, "cloves": 5, "leaf": 2, "leaves": 2, "sprig": 2, "sprigs": 2,
    "pinch": 1, "to": 2,  # "to taste" -> first_word "to"
    "slice": 25, "slices": 25, "can": 400, "tin": 400,
    "bunch": 60, "handful": 30, "wedge": 30, "wedges": 30,
    "sheet": 10, "sheets": 10, "stalk": 40, "stalks": 40,
    "fillet": 150, "fillets": 150, "portion": 150, "pack": 200, "head": 500,
}
CUP_GRAMS_BY_KEYWORD: dict[str, float] = {"flour": 120, "sugar": 200, "rice": 185, "oats": 90}
DEFAULT_CUP_GRAMS = 150
WHOLE_GRAMS_BY_KEYWORD: dict[str, float] = {
    "egg": 50, "onion": 150, "garlic": 5, "potato": 170, "tomato": 120,
    "lemon": 60, "lime": 45, "avocado": 170, "carrot": 60, "pepper": 120, "banana": 120,
}
DEFAULT_WHOLE_GRAMS = 100


def _kcal_per_gram_for(name: str) -> float:
    name = name.lower()
    for keyword, value in KCAL_PER_GRAM.items():
        if keyword in name:
            return value
    return DEFAULT_KCAL_PER_GRAM


def _estimate_grams(ingredient: dict) -> tuple[float, bool]:
    """Returns (estimated grams, matched) — matched=False means a generic
    per-count fallback was used rather than a real unit/category match."""
    qty = float(ingredient.get("qty") or 0)
    unit = (ingredient.get("unit") or "").lower().strip()
    name = (ingredient.get("name") or "").lower()
    head = unit.split("(")[0].strip()
    first_word = head.split()[0] if head else ""

    if first_word in ("g", "grams", "gram", "ml", "milliliters", "milliliter"):
        return qty, True
    if first_word == "kg":
        return qty * 1000, True
    if first_word == "l":
        return qty * 1000, True
    if first_word in ("cup", "cups"):
        for keyword, grams in CUP_GRAMS_BY_KEYWORD.items():
            if keyword in name:
                return qty * grams, True
        return qty * DEFAULT_CUP_GRAMS, True
    if first_word in GRAMS_PER_UNIT:
        return qty * GRAMS_PER_UNIT[first_word], True
    for keyword, grams in WHOLE_GRAMS_BY_KEYWORD.items():
        if keyword in name:
            return qty * grams, True
    return qty * DEFAULT_WHOLE_GRAMS, False


def _finding(recipe_id, check_name: str, detail: str, title: str | None = None) -> dict:
    return {"recipe_id": recipe_id, "check_name": check_name, "detail": detail, "title": title}


async def _structural_findings() -> list[dict]:
    """Ports the ad-hoc scan script used by this session's manual audit into
    a permanent, reusable check — deliberately reuses the SAME functions
    Part B wired into generation (validate_ingredient_units, validate_steps)
    so this can never silently drift from what generation itself enforces."""
    findings: list[dict] = []
    rows = await db.pool().fetch(
        "SELECT id, title, status, ingredients, kcal, time, steps FROM recipes"
    )
    for row in rows:
        recipe_id, title = row["id"], row["title"]
        raw_ingredients = row["ingredients"] or []

        if raw_ingredients and not isinstance(raw_ingredients, list):
            # Same double-encoded-JSON bug class as steps_not_array below —
            # found live by this check (2026-07-14), and much larger in
            # scope: 63/167 recipes, including ~40% of ALL openrouter-sourced
            # rows, not just old bulk-seeded data. Report once and treat as
            # no ingredients for the rest of this loop iteration — iterating
            # a string here would walk it character-by-character against
            # Ingredient(**ing), producing hundreds of bogus findings per row
            # (a real bug in this function's first version). kcal/time/steps
            # checks below are independent of ingredient shape, so they
            # still run for this row.
            findings.append(
                _finding(
                    recipe_id,
                    "ingredients_not_array",
                    f"ingredients is stored as {type(raw_ingredients).__name__}, not a "
                    "list — likely double-encoded JSON",
                    title,
                )
            )
            ingredients = []
        else:
            ingredients = raw_ingredients
        for ing in ingredients:
            try:
                Ingredient(**ing)
            except (ValidationError, TypeError) as exc:
                findings.append(
                    _finding(recipe_id, "ingredient_shape", f"{ing!r}: {exc}", title)
                )

        # Everything below assumes dict-shaped ingredients (.get/[...]) — a
        # malformed element (e.g. a bare string) is already reported by the
        # shape check above; skip it here rather than crashing the rest of
        # the audit over a row that's already flagged.
        shaped = [ing for ing in ingredients if isinstance(ing, dict)]

        if row["status"] != "stub":
            if not row["kcal"]:
                findings.append(_finding(recipe_id, "missing_kcal", "kcal is null/0", title))
            if not row["time"]:
                findings.append(_finding(recipe_id, "missing_time", "time is null/empty", title))
            for ing in shaped:
                if "allergen_tags" not in ing:
                    findings.append(
                        _finding(
                            recipe_id, "missing_allergen_tags", ing.get("name", "?"), title
                        )
                    )

        for ing in shaped:
            try:
                validate_ingredient_units([ing])
            except GeneratedRecipeInvalid as exc:
                findings.append(_finding(recipe_id, "bad_unit", str(exc), title))

        steps = row["steps"] or []
        if steps and not isinstance(steps, list):
            # Real, previously-unknown data bug found live by this check
            # (2026-07-14): some rows have `steps` stored as a JSON STRING
            # (double-encoded — asyncpg's jsonb codec was given an
            # already-json.dumps'd string somewhere instead of a native
            # list, so it re-encoded that string as a jsonb scalar) rather
            # than a native jsonb array. Flagged, not auto-fixed — see Part C
            # design principle.
            findings.append(
                _finding(
                    recipe_id,
                    "steps_not_array",
                    f"steps is stored as {type(steps).__name__}, not a list — likely "
                    "double-encoded JSON",
                    title,
                )
            )
        elif steps:
            try:
                validate_steps(steps)
            except GeneratedStepsInvalid as exc:
                findings.append(_finding(recipe_id, "step_format", str(exc), title))

    return findings


def evaluate_kcal_plausibility(
    ingredients: list, stored_kcal: int | None, base_serves: int | None
) -> list[tuple[str, str]]:
    """Pure, DB-free version of the per-recipe kcal-plausibility logic —
    extracted 2026-07-15 so the nightly audit (_kcal_findings below) and the
    paired golden-set test (tests/test_golden_set_quality.py) share one
    implementation instead of two copies that could silently drift, exactly
    the failure mode Part A's generation_rules.py was built to prevent for
    prompts. Returns (check_name, detail) pairs, empty if everything passes.
    Preserves the original control flow: an implausible ratio short-circuits
    the other two checks for this recipe (same as the pre-extraction code's
    `continue`), since a wildly-off estimate makes the range/round checks
    redundant noise on top of an already-flagged row."""
    results: list[tuple[str, str]] = []
    # Malformed (non-dict) ingredients are already reported by the
    # structural check — skip them here rather than crashing.
    shaped = [ing for ing in (ingredients or []) if isinstance(ing, dict)]
    if not shaped or not stored_kcal or stored_kcal <= 0:
        return results

    estimated = 0.0
    matched_mass = 0.0
    total_mass = 0.0
    for ing in shaped:
        grams, matched = _estimate_grams(ing)
        estimated += grams * _kcal_per_gram_for(ing.get("name") or "")
        total_mass += grams
        if matched:
            matched_mass += grams
    if total_mass <= 0 or estimated <= 0:
        return results

    unmatched_ratio = 1 - (matched_mass / total_mass)
    threshold = (
        KCAL_RATIO_THRESHOLD_LOOSE
        if unmatched_ratio > UNMATCHED_MASS_TOLERANCE
        else KCAL_RATIO_THRESHOLD
    )

    ratio = max(stored_kcal, estimated) / max(min(stored_kcal, estimated), 1)
    if ratio > threshold:
        results.append(
            ("kcal_implausible", f"stored={stored_kcal} vs rough estimate={round(estimated)} ({ratio:.1f}x off)")
        )
        return results

    serves = base_serves or 1
    per_serving = stored_kcal / serves
    if not (MIN_PLAUSIBLE_KCAL_PER_SERVING <= per_serving <= MAX_PLAUSIBLE_KCAL_PER_SERVING):
        results.append(
            ("kcal_out_of_range", f"{per_serving:.0f} kcal/serving (stored={stored_kcal}, base_serves={serves})")
        )

    return results


# Smallest real cluster found in the original placeholder discovery (three
# unrelated recipes independently at exactly 1200 kcal; fifteen at 420, four
# at 320) — impossible by chance for genuine per-ingredient computation.
KCAL_DUPLICATE_CLUSTER_MIN_SIZE = 3


def find_kcal_duplicates(recipes: list[dict], min_cluster_size: int = KCAL_DUPLICATE_CLUSTER_MIN_SIZE):
    """Groups recipes by their exact stored kcal, keeping only values shared
    by min_cluster_size+ recipes. This is the real signal behind
    kcal_suspiciously_round — first tried as a simple per-recipe "is this
    divisible by 20" check, but that flagged 95/166 recipes on a real
    full-pool run (2026-07-15), mostly individually-correct totals that
    just happened to round cleanly (this session's own manual kcal fixes
    included). The actual tell in the original discovery was never
    roundness alone, it was many UNRELATED dishes sharing one EXACT total —
    a coincidence real ingredient math essentially can't produce."""
    by_kcal: dict[int, list[dict]] = {}
    for r in recipes:
        if r.get("kcal"):
            by_kcal.setdefault(r["kcal"], []).append(r)
    return {kcal: group for kcal, group in by_kcal.items() if len(group) >= min_cluster_size}


async def _kcal_findings() -> list[dict]:
    findings: list[dict] = []
    rows = await db.pool().fetch(
        "SELECT id, title, ingredients, kcal, base_serves FROM recipes "
        "WHERE status != 'stub' AND kcal IS NOT NULL AND kcal > 0"
    )
    for row in rows:
        for check_name, detail in evaluate_kcal_plausibility(
            row["ingredients"], row["kcal"], row["base_serves"]
        ):
            findings.append(_finding(row["id"], check_name, detail, row["title"]))

    for kcal_value, cluster in find_kcal_duplicates([dict(r) for r in rows]).items():
        for row in cluster:
            findings.append(
                _finding(
                    row["id"],
                    "kcal_suspiciously_round",
                    f"{kcal_value} kcal, shared with {len(cluster) - 1} other unrelated recipe(s)",
                    row["title"],
                )
            )

    return findings


async def _get_watermark() -> datetime:
    row = await db.pool().fetchrow(
        "SELECT checked_through FROM recipe_audit_watermark WHERE key = 'semantic_check'"
    )
    return row["checked_through"] if row else datetime(1970, 1, 1, tzinfo=UTC)


async def _set_watermark(ts: datetime) -> None:
    await db.pool().execute(
        """
        INSERT INTO recipe_audit_watermark (key, checked_through, updated_at)
        VALUES ('semantic_check', $1, now())
        ON CONFLICT (key) DO UPDATE SET checked_through = $1, updated_at = now()
        """,
        ts,
    )


# Calibrated after the first real run flagged 125/125 checked recipes,
# including ones with every defining ingredient present (e.g. a tofu pad
# thai with tamarind paste AND palm sugar AND soy sauce still got flagged
# for lacking garlic/lime/chili-flakes/shallots) — the free model defaults
# to nitpicky when asked an open-ended "is anything missing" question. This
# version explicitly separates "unrecognizable without it" from "a common
# but swappable extra" and asks it to default to ok:true when uncertain.
#
# Recalibrated again (2026-07-14, second pass) after a full-pool run showed
# two more failure modes even with the above: (1) self-referential noise —
# "Risotto" flagged for missing "risotto", "Fried Rice" for missing "fried
# rice" — the model treating the dish's own category name as a missing
# ingredient; (2) multi-sentence rambling replacing a short item name (seen
# on Spaghetti Aglio e Olio: a 700-character stream-of-consciousness "missing"
# entry). Both addressed below; _filter_self_contradicting below is the
# separate, deterministic backstop for a THIRD failure mode this same run
# confirmed — Chicken Pad Thai flagged for "missing: tamarind paste" while
# tamarind paste was literally already in its ingredient list.
SEMANTIC_SYSTEM_PROMPT = (
    "You are a culinary expert doing a LIGHT sanity check on recipes for a meal-planning app. "
    "Most recipes you review are already correct — your job is to catch only the rare, obvious "
    "case where a dish is missing the ONE component that makes it recognizably that dish (e.g. "
    "carbonara with no egg or no cheese, a curry with no curry paste/spice base, paella with no "
    "rice or no saffron, pad thai with no tamarind/fish-sauce-style sauce base, a cheese-named "
    "dish missing that cheese). "
    "Do NOT flag minor, swappable, or optional details: garnishes, plain aromatics like garlic/"
    "onion/ginger, a substituted seasoning (e.g. soy sauce instead of fish sauce in a vegetarian "
    "version), a missing side, or ordinary stylistic variation. "
    "Never list the dish's own name or category as a missing ingredient (e.g. never answer "
    "'risotto' is missing from a risotto, or 'tacos' from tacos, or 'fried rice' from a fried "
    "rice) — a dish cannot be missing itself, only a specific component within it. "
    "Each entry in missing must be a short ingredient or component name only (2-5 words) — "
    "never a sentence, never an explanation, never reasoning about the answer. "
    "If you are not CERTAIN the dish is unrecognizable without something, answer ok: true."
)


def _filter_self_contradicting(missing: list[str], ingredient_names: list[str]) -> list[str]:
    """Deterministic backstop, not a judgment call: found live (2026-07-14)
    that the model flagged Chicken Pad Thai for 'missing: tamarind paste'
    while tamarind paste was already, literally, in its ingredient list. A
    claim whose own significant words are already present among the recipe's
    real ingredient names is a self-contradiction — drop it before it's ever
    persisted as a finding, rather than trusting the model's claim blindly."""
    haystack = " | ".join(name.lower() for name in ingredient_names)
    kept = []
    for item in missing:
        # Only check the claim itself, not any trailing explanation the
        # model tacked on (rare now given the length instruction above, but
        # cheap to guard against).
        head = re.split(r"[,.]| — | - ", item, maxsplit=1)[0]
        words = [w for w in re.findall(r"[a-zA-Z]+", head.lower()) if len(w) > 3]
        if words and all(w in haystack for w in words):
            continue
        kept.append(item)
    return kept


async def _check_defining_ingredients(row) -> str | None:
    """One free cloudflare.generate_text call — the detective half of the
    defining-ingredient gap (generation_rules.DEFINING_COMPONENTS_RULE is
    the preventive half). Raises on any provider/parse failure; the caller
    decides whether to advance the watermark past this row."""
    ingredients = row["ingredients"]
    if not isinstance(ingredients, list) or not ingredients:
        # Already reported by the structural scan (ingredients_not_array /
        # missing ingredients) — no signal to check here, and sending an
        # empty list to the model would just manufacture a guaranteed
        # false "missing everything" flag.
        return None

    ingredient_names = [
        i.get("name", "") for i in ingredients if isinstance(i, dict) and i.get("name")
    ]
    names = ", ".join(ingredient_names)
    if not names:
        return None

    prompt = (
        f'Dish: "{row["title"]}" ({row["cuisine"] or "unspecified cuisine"}).\n'
        f"Ingredients: {names}\n\n"
        "Is this dish missing the one component that defines it as this specific dish? "
        'Reply with ONLY a JSON object: {"ok": true|false, "missing": ["..."]}. '
        "missing should list only genuinely dish-defining components, empty if ok is true."
    )
    raw = await cloudflare.generate_text(SEMANTIC_SYSTEM_PROMPT, prompt)
    try:
        result = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        # The free model occasionally returns non-JSON text (seen live,
        # ~1 in 20 calls) — a per-row quirk, not a provider outage. Treat as
        # "couldn't determine" rather than letting it kill the whole batch
        # and stall the watermark on an otherwise-healthy provider.
        return None

    missing = _filter_self_contradicting(result.get("missing") or [], ingredient_names)
    if result.get("ok", True) or not missing:
        return None
    return f"missing: {', '.join(missing)}"


async def _semantic_findings() -> dict:
    """Bounded to recipes created since the last successful watermark — the
    only cost-bearing check here (even though cloudflare.generate_text is
    free, it's still a live call per recipe), so it must not re-scan the
    whole pool every night. On any failure, the watermark simply doesn't
    advance past the unattempted rows — the same batch is retried next run
    rather than silently skipping recipes a down provider couldn't reach."""
    watermark = await _get_watermark()
    rows = await db.pool().fetch(
        """
        SELECT id, title, cuisine, ingredients, created_at FROM recipes
        WHERE status != 'stub' AND created_at > $1
        ORDER BY created_at ASC
        """,
        watermark,
    )

    findings: list[dict] = []
    checked = 0
    error = None
    newest_checked = watermark
    for row in rows:
        try:
            missing = await _check_defining_ingredients(row)
        except Exception as exc:  # noqa: BLE001 — stop here, retry this batch next run
            error = str(exc)
            break
        checked += 1
        newest_checked = row["created_at"]
        if missing:
            findings.append(_finding(row["id"], SEMANTIC_CHECK_NAME, missing, row["title"]))

    if checked:
        await _set_watermark(newest_checked)

    return {"findings": findings, "checked": checked, "error": error}


async def _persist_findings(findings: list[dict]) -> list[dict]:
    """Same upsert-dedup philosophy as unmapped_ingredients — a finding
    already on record from a previous run doesn't re-fire the email every
    night. ON CONFLICT DO NOTHING + RETURNING id distinguishes new rows from
    already-known ones (fetchval is None on conflict)."""
    new_findings = []
    for f in findings:
        inserted_id = await db.pool().fetchval(
            """
            INSERT INTO recipe_audit_findings (recipe_id, check_name, detail)
            VALUES ($1, $2, $3)
            ON CONFLICT (recipe_id, check_name, detail) DO NOTHING
            RETURNING id
            """,
            f["recipe_id"],
            f["check_name"],
            f["detail"],
        )
        if inserted_id is not None:
            new_findings.append(f)
    return new_findings


async def _send_findings_email(findings: list[dict]) -> None:
    items = "".join(
        f"<li><strong>{f['title'] or f['recipe_id']}</strong> "
        f"[{f['check_name']}]: {f['detail']}</li>"
        for f in findings
    )
    await resend_client.send_email(
        f"mesa: recipe audit found {len(findings)} new finding(s)",
        f"<p>{len(findings)} new recipe quality finding(s) this run.</p>"
        f"<ul>{items}</ul>"
        "<p>Review via recipe_audit_findings in Supabase. Findings are flagged for human "
        "review only — never auto-fixed.</p>",
    )


async def run_recipe_audit() -> dict:
    structural = await _structural_findings()
    kcal = await _kcal_findings()
    semantic = await _semantic_findings()

    all_findings = structural + kcal + semantic["findings"]
    new_findings = await _persist_findings(all_findings)

    if new_findings:
        await _send_findings_email(new_findings)

    return {
        "audit_findings_total": len(all_findings),
        "audit_findings_new": len(new_findings),
        "audit_structural_flags": len(structural),
        "audit_kcal_flags": len(kcal),
        "audit_semantic_checked": semantic["checked"],
        "audit_semantic_flags": len(semantic["findings"]),
        "audit_semantic_error": semantic["error"],
    }


_TIER_CHECK_NAMES = {
    "deterministic": provider_quality.DETERMINISTIC_CHECKS,
    "heuristic": provider_quality.HEURISTIC_CHECKS,
    "semantic": provider_quality.SEMANTIC_CHECKS,
}


async def list_findings(
    resolved: bool = False,
    provider: str | None = None,
    tier: str | None = None,
    check_name: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Part 2 of the 2026-07-15 quality-monitoring work — until this
    existed, the only way to resolve a finding was a manual Supabase edit;
    there was no code path that ever set resolved=true. Filterable backlog
    view + a summary block, since "how big is the backlog and is it
    growing" is the actual operational question, not just the raw list."""
    conditions = ["f.resolved = $1"]
    params: list = [resolved]

    if provider:
        params.append(provider)
        conditions.append(f"r.source = ${len(params)}")
    if check_name:
        params.append(check_name)
        conditions.append(f"f.check_name = ${len(params)}")
    elif tier:
        names = _TIER_CHECK_NAMES.get(tier)
        if not names:
            raise ValueError(f"Unknown tier '{tier}' — expected one of {list(_TIER_CHECK_NAMES)}")
        params.append(list(names))
        conditions.append(f"f.check_name = ANY(${len(params)}::text[])")

    where_clause = " AND ".join(conditions)
    params.extend([limit, offset])
    rows = await db.pool().fetch(
        f"""
        SELECT f.id, f.recipe_id, r.title, r.source AS provider, f.check_name, f.detail,
               f.created_at, f.resolved, f.resolved_at, f.resolution_note
        FROM recipe_audit_findings f
        JOIN recipes r ON r.id = f.recipe_id
        WHERE {where_clause}
        ORDER BY f.created_at DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )

    summary = await db.pool().fetchrow(
        """
        SELECT
            count(*) FILTER (WHERE NOT resolved) AS open_total,
            count(*) FILTER (WHERE NOT resolved AND created_at >= now() - interval '7 days')
                AS opened_this_week,
            count(*) FILTER (WHERE resolved AND resolved_at >= now() - interval '7 days')
                AS resolved_this_week
        FROM recipe_audit_findings
        """
    )

    return {"findings": [dict(r) for r in rows], "summary": dict(summary)}


async def resolve_finding(finding_id: str, note: str | None = None) -> dict | None:
    row = await db.pool().fetchrow(
        """
        UPDATE recipe_audit_findings
        SET resolved = true, resolved_at = now(), resolution_note = $1
        WHERE id = $2
        RETURNING id, recipe_id, check_name, detail, resolved, resolved_at, resolution_note
        """,
        note,
        finding_id,
    )
    return dict(row) if row else None
