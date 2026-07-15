"""Part of the 2026-07-15 production-readiness investigation — answers "is
Groq/OpenRouter good enough for prod" from real data, not impression. Two
sub-reports that are deliberately never blended into one number:

- reliability (attempt-level: calls, errors, latency) from prompt_audit_log,
  which has captured this data since day one but nothing ever aggregated it.
- content_quality (recipe-level defect rate) from recipe_audit_findings,
  joined against recipes.source now that source actually reflects which
  provider answered (see ai_client.chat_completion's (response, provider)
  return contract, added the same day this module was).

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
SEMANTIC_CHECKS = frozenset({"missing_defining_ingredient"})

# Only the two providers this comparison is actually about — cloudflare
# (stubs) and the legacy claude/gemini seed data predate this pipeline and
# would just be noise in a groq-vs-openrouter report.
COMPARABLE_PROVIDERS = ("groq", "openrouter")

# Semantic check precision, measured by hand this session (2026-07-14): 6
# "high-confidence-looking" findings individually verified against real
# ingredient data, only 2 held up. Surfaced as a caveat, not baked into any
# rate calculation — the raw count itself is still useful as a trend signal.
SEMANTIC_CHECK_MEASURED_PRECISION = "~33% (6 spot-checked, 2 confirmed real, 2026-07-14)"


async def get_reliability_report(since_days: int = 30) -> list[dict]:
    """Attempt-level: every prompt_audit_log row IS this data, already
    captured on every call via ai_client._log_call — just never queried
    before now. Scoped to recipe-creation calls (generate_recipes,
    stub_expansion) since steps/translate always use openrouter_only_
    completion and would just show 100% openrouter, not a real comparison."""
    since = datetime.now(UTC) - timedelta(days=since_days)
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
          AND (model LIKE '%:generate_recipes' OR model LIKE '%:stub_expansion')
          AND split_part(model, ':', 1) = ANY($2::text[])
        GROUP BY 1
        ORDER BY 1
        """,
        since,
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


async def get_content_quality_report(since_days: int = 30) -> dict:
    """Recipe-level defect rate by provider, tiered. Only trustworthy for
    recipes created after the source-attribution fix (2026-07-15) — older
    rows were best-effort backfilled from prompt_audit_log, not guaranteed."""
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
        GROUP BY 1, 2
        """,
        since,
        list(COMPARABLE_PROVIDERS),
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


async def get_provider_quality_scorecard(since_days: int = 30) -> dict:
    return {
        "window_days": since_days,
        "reliability": await get_reliability_report(since_days),
        "content_quality": await get_content_quality_report(since_days),
        "caveats": [
            "recipes.source is only guaranteed-accurate for rows created after 2026-07-15 "
            "(the provider-attribution fix) — older rows were best-effort backfilled from "
            "prompt_audit_log and may be wrong for recipes with multiple logged attempts.",
            f"missing_defining_ingredient (semantic tier) measured precision: "
            f"{SEMANTIC_CHECK_MEASURED_PRECISION} — do not use its count as a quality gate on "
            "its own.",
            "reliability.error_rate_pct counts every attempt, including ones Groq failing over "
            "to OpenRouter within the same request — it is NOT the same as user-facing failure "
            "rate.",
            "Groq is tried first in chat_completion() and OpenRouter only serves the residual "
            "cases Groq already failed on — organic traffic here is NOT a matched sample "
            "between providers. See the paired golden-set test for an unconfounded comparison.",
            "content_quality only counts UNRESOLVED findings — a finding fixed by editing the "
            "recipe doesn't count against the provider forever, but this also means the rate "
            "understates total defects ever produced, not just currently-open ones.",
        ],
    }
