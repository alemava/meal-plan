"""Part of the 2026-07-15 production-readiness investigation — answers "is
Groq/OpenRouter good enough for prod" from real data, not impression.

Two PIPELINE STAGES are tracked separately, not blended, because they're
answered by different providers under different rules: recipe suggestion
(ingredients/kcal/etc — Groq first, OpenRouter fallback, via
ai_client.chat_completion) and steps generation (OpenRouter only, by
deliberate design — ai_client.openrouter_only_completion). A recipe's
`source` column reflects who suggested it; a SEPARATE `steps_source` column
(added the same day as this stage split, 2026-07-15) reflects who wrote its
steps — these can differ on the same row (e.g. Groq suggests the recipe,
OpenRouter writes its steps, the normal case), so a finding about broken
steps must never be attributed to `source`, only to `steps_source`.

Within each stage, two sub-reports that are deliberately never blended into
one number:
- reliability (attempt-level: calls, errors, latency) from prompt_audit_log,
  which has captured this data since day one but nothing ever aggregated it.
- content_quality (recipe-level defect rate) from recipe_audit_findings,
  tiered by trustworthiness (deterministic/heuristic/semantic — see below).

Findings are tiered exactly like recipe_audit.py treats them: deterministic
checks are the same validators that already block generation, so any
non-zero rate at real volume is real signal. Heuristic checks are
correlational (kcal plausibility). The semantic check (missing_defining_
ingredient) is reported as a raw count only, NEVER as a rate — this
session measured its real precision at roughly 33% even on curated
candidates, so treating its flag count as a quality signal would be
comparing noise, not providers."""

from datetime import UTC, datetime, timedelta

from app.core import db

DETERMINISTIC_CHECKS = frozenset(
    {
        "ingredients_not_array",
        "ingredient_shape",
        "missing_kcal",
        "missing_time",
        "missing_allergen_tags",
        "bad_unit",
        "steps_not_array",
        "step_format",
    }
)
HEURISTIC_CHECKS = frozenset({"kcal_implausible", "kcal_out_of_range", "kcal_suspiciously_round"})
SEMANTIC_CHECKS = frozenset({"missing_defining_ingredient", "missing_natural_variation"})

# Orthogonal to the trust tiers above: WHICH pipeline stage a check is about,
# used only to decide whether a finding attributes to `source` (recipe
# suggestion) or `steps_source` (steps generation). Both current members are
# also in DETERMINISTIC_CHECKS — there's no heuristic/semantic check for
# steps content today, so the steps content-quality report only ever shows
# deterministic numbers, honestly, rather than forcing empty tiers.
STEPS_STAGE_CHECKS = frozenset({"steps_not_array", "step_format"})

# groq/openrouter data freezes as of 2026-07-16 (both retired from the live
# waterfall in favour of openrouter_paid/deepinfra — see ai_client.py) but
# stays here rather than being swapped out: an implicit before/after on the
# same report, and steps generation's tiny real historical groq slice
# (2 calls, 2026-07-10) still shouldn't be silently dropped. openrouter_paid/
# deepinfra are the ones with an ongoing go/no-go question now.
COMPARABLE_PROVIDERS = ("groq", "openrouter", "openrouter_paid", "deepinfra")

# Semantic check precision, measured by hand this session (2026-07-14): 6
# "high-confidence-looking" findings individually verified against real
# ingredient data, only 2 held up. Surfaced as a caveat, not baked into any
# rate calculation — the raw count itself is still useful as a trend signal.
SEMANTIC_CHECK_MEASURED_PRECISION = "~33% (6 spot-checked, 2 confirmed real, 2026-07-14)"


async def _reliability_report(since_days: int, purposes: list[str]) -> list[dict]:
    """Shared query shape for both pipeline stages — every prompt_audit_log
    row already carries latency_ms/tokens_in/tokens_out/error on every call
    via ai_client._log_call, just never aggregated before this module.
    purposes are OR'd together (e.g. recipe suggestion spans both
    generate_recipes and stub_expansion)."""
    since = datetime.now(UTC) - timedelta(days=since_days)
    patterns = [f"%:{p}" for p in purposes]
    rows = await db.pool().fetch(
        """
        SELECT split_part(model, ':', 1) AS provider,
               count(*) AS calls,
               count(*) FILTER (WHERE error IS NOT NULL) AS errors,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_latency_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms,
               avg(coalesce(tokens_in, 0) + coalesce(tokens_out, 0)) AS avg_tokens
        FROM prompt_audit_log
        WHERE created_at >= $1
          AND model LIKE ANY($2::text[])
          AND split_part(model, ':', 1) = ANY($3::text[])
        GROUP BY 1
        ORDER BY 1
        """,
        since,
        patterns,
        list(COMPARABLE_PROVIDERS),
    )
    return [
        {
            "provider": r["provider"],
            "calls": r["calls"],
            "errors": r["errors"],
            "error_rate_pct": round(100 * r["errors"] / r["calls"], 1) if r["calls"] else None,
            "p50_latency_ms": round(r["p50_latency_ms"]) if r["p50_latency_ms"] is not None else None,
            "p95_latency_ms": round(r["p95_latency_ms"]) if r["p95_latency_ms"] is not None else None,
            "avg_tokens_per_call": round(r["avg_tokens"]) if r["avg_tokens"] is not None else None,
        }
        for r in rows
    ]


async def get_reliability_report(since_days: int = 30) -> list[dict]:
    """Recipe-suggestion stage only (generate_recipes, stub_expansion) —
    the stage with a real Groq-vs-OpenRouter choice to compare. Steps
    generation is reported separately by get_steps_reliability_report,
    since it isn't a comparison (OpenRouter only, by design) but is still
    worth measuring on its own."""
    return await _reliability_report(since_days, ["generate_recipes", "stub_expansion"])


async def get_steps_reliability_report(since_days: int = 30) -> list[dict]:
    """Steps-generation stage (select_recipe_steps) — not a comparison
    (OpenRouter only today, by deliberate design; see ai_client.
    openrouter_only_completion), but tracked on its own since it was
    previously invisible in any report: the original reliability report
    explicitly excluded this purpose. A tiny amount of real historical Groq
    data exists too (2 calls, 2026-07-10, predating the openrouter-only
    design being finalized) — surfaced here rather than silently dropped."""
    return await _reliability_report(since_days, ["select_recipe_steps"])


async def get_content_quality_report(since_days: int = 30) -> dict:
    """Recipe-suggestion stage only (ingredients/kcal), tiered. Excludes
    STEPS_STAGE_CHECKS — those attribute to steps_source, not source, and
    are reported separately by get_steps_content_quality_report. Only
    trustworthy for recipes created after the source-attribution fix
    (2026-07-15) — older rows were best-effort backfilled from
    prompt_audit_log, not guaranteed."""
    since = datetime.now(UTC) - timedelta(days=since_days)

    recipe_counts = await db.pool().fetch(
        """
        SELECT source AS provider, count(*) AS recipe_count
        FROM recipes
        WHERE created_at >= $1 AND status != 'stub' AND source = ANY($2::text[])
        GROUP BY 1
        """,
        since,
        list(COMPARABLE_PROVIDERS),
    )
    counts = {r["provider"]: r["recipe_count"] for r in recipe_counts}

    # resolved = false only — an already-fixed finding shouldn't count
    # against a provider's current defect rate forever. This matters
    # concretely: 108 of this session's findings (ingredients_not_array/
    # steps_not_array) were caused by this session's own ad-hoc repair
    # scripts double-encoding JSON, not by generation at all — counting
    # those against a provider would be measuring the wrong thing entirely.
    finding_rows = await db.pool().fetch(
        """
        SELECT r.source AS provider, f.check_name, count(*) AS finding_count
        FROM recipe_audit_findings f
        JOIN recipes r ON r.id = f.recipe_id
        WHERE r.created_at >= $1 AND r.source = ANY($2::text[]) AND f.resolved = false
          AND NOT (f.check_name = ANY($3::text[]))
        GROUP BY 1, 2
        """,
        since,
        list(COMPARABLE_PROVIDERS),
        list(STEPS_STAGE_CHECKS),
    )

    report: dict = {}
    for provider in COMPARABLE_PROVIDERS:
        recipe_count = counts.get(provider, 0)
        deterministic = sum(
            r["finding_count"]
            for r in finding_rows
            if r["provider"] == provider and r["check_name"] in DETERMINISTIC_CHECKS
        )
        heuristic = sum(
            r["finding_count"]
            for r in finding_rows
            if r["provider"] == provider and r["check_name"] in HEURISTIC_CHECKS
        )
        semantic = sum(
            r["finding_count"]
            for r in finding_rows
            if r["provider"] == provider and r["check_name"] in SEMANTIC_CHECKS
        )
        report[provider] = {
            "recipe_count": recipe_count,
            "deterministic_defect_rate_pct": (
                round(100 * deterministic / recipe_count, 2) if recipe_count else None
            ),
            "heuristic_flag_rate_pct": (
                round(100 * heuristic / recipe_count, 2) if recipe_count else None
            ),
            "semantic_findings_count": semantic,
            "semantic_findings_note": (
                "raw count only, NOT a rate — see SEMANTIC_CHECK_MEASURED_PRECISION"
            ),
        }
    return report


async def get_steps_content_quality_report(since_days: int = 30) -> dict:
    """Steps-generation stage only — the mirror of get_content_quality_
    report, joined against steps_source instead of source. Only covers
    STEPS_STAGE_CHECKS (steps_not_array, step_format); there's no heuristic
    or semantic check for step content today, so this only ever reports a
    deterministic rate — honest about that rather than forcing empty tiers."""
    since = datetime.now(UTC) - timedelta(days=since_days)

    recipe_counts = await db.pool().fetch(
        """
        SELECT steps_source AS provider, count(*) AS recipe_count
        FROM recipes
        WHERE created_at >= $1 AND status = 'complete' AND steps_source = ANY($2::text[])
        GROUP BY 1
        """,
        since,
        list(COMPARABLE_PROVIDERS),
    )
    counts = {r["provider"]: r["recipe_count"] for r in recipe_counts}

    finding_rows = await db.pool().fetch(
        """
        SELECT r.steps_source AS provider, count(*) AS finding_count
        FROM recipe_audit_findings f
        JOIN recipes r ON r.id = f.recipe_id
        WHERE r.created_at >= $1 AND r.steps_source = ANY($2::text[]) AND f.resolved = false
          AND f.check_name = ANY($3::text[])
        GROUP BY 1
        """,
        since,
        list(COMPARABLE_PROVIDERS),
        list(STEPS_STAGE_CHECKS),
    )
    findings_by_provider = {r["provider"]: r["finding_count"] for r in finding_rows}

    report: dict = {}
    for provider in COMPARABLE_PROVIDERS:
        recipe_count = counts.get(provider, 0)
        defects = findings_by_provider.get(provider, 0)
        report[provider] = {
            "recipe_count": recipe_count,
            "deterministic_defect_rate_pct": (
                round(100 * defects / recipe_count, 2) if recipe_count else None
            ),
        }
    return report


async def get_provider_quality_scorecard(since_days: int = 30) -> dict:
    return {
        "window_days": since_days,
        "recipe_suggestion": {
            "reliability": await get_reliability_report(since_days),
            "content_quality": await get_content_quality_report(since_days),
        },
        "steps_generation": {
            "reliability": await get_steps_reliability_report(since_days),
            "content_quality": await get_steps_content_quality_report(since_days),
        },
        "caveats": [
            "recipes.source/steps_source are only guaranteed-accurate for rows created after "
            "2026-07-15 (the provider-attribution fixes) — older rows were best-effort "
            "backfilled from prompt_audit_log (falling back to source itself for legacy rows "
            "with no logged steps call at all, e.g. the original claude/gemini seed data).",
            f"missing_defining_ingredient (semantic tier, recipe_suggestion only) measured "
            f"precision: {SEMANTIC_CHECK_MEASURED_PRECISION} — do not use its count as a "
            "quality gate on its own.",
            "reliability.error_rate_pct counts every attempt, including ones Groq failing over "
            "to OpenRouter within the same request — it is NOT the same as user-facing failure "
            "rate.",
            "Groq is tried first in chat_completion() and OpenRouter only serves the residual "
            "cases Groq already failed on — organic recipe_suggestion traffic here is NOT a "
            "matched sample between providers. See the paired golden-set test for an "
            "unconfounded comparison. steps_generation has no such comparison to confound — "
            "it's OpenRouter by design, reported for its own sake, not vs. Groq.",
            "content_quality only counts UNRESOLVED findings — a finding fixed by editing the "
            "recipe doesn't count against the provider forever, but this also means the rate "
            "understates total defects ever produced, not just currently-open ones.",
        ],
    }
