import html
import math
import re

from app.services import ai_client

# 2026-07-20 — rough per-step estimate for a step with NO timer (mixing,
# chopping, plating, preheating). Prep time is genuinely hard to estimate
# and the model doesn't timer it; a modest flat floor is honest-ish and,
# critically, keeps timer steps (the concrete, dominant durations) from
# being drowned out. See _step_costs_minutes / timer_aware_total_minutes.
PREP_STEP_MINUTES = 2

# step.text is rendered as raw HTML by the frontend (to support <strong>
# ingredient highlighting) — escape everything else so a prompt injection or
# plain hallucination can never inject a real tag (script, img/onerror, etc.)
# into the page. Same "don't trust the model" philosophy as the allergen
# safety net: sanitize the model's own output, not just its inputs.
_ALLOWED_TAG_PATTERN = re.compile(r"&lt;(/?)strong&gt;", re.IGNORECASE)

# Matches any real HTML tag — used post-sanitization, where only <strong>
# should still exist as a literal tag (everything else was escaped away).
_ANY_TAG_PATTERN = re.compile(r"</?([a-zA-Z]+)[^>]*>")


class GeneratedStepsInvalid(Exception):
    """Reject-and-regenerate contract, same as guardrails.GeneratedRecipeInvalid
    — raised by validate_steps, deliberately left uncaught in generate_steps
    so it propagates to the caller's own retry mechanism (the Cloud Tasks
    worker in internal.py never catches exceptions from generate_steps, by
    design, so Cloud Tasks' own retry policy re-dispatches the whole task)."""


def sanitize_step_text(text: str) -> str:
    escaped = html.escape(text, quote=False)
    return _ALLOWED_TAG_PATTERN.sub(lambda m: f"<{m.group(1)}strong>", escaped)


def _step_costs_minutes(steps: list[dict]) -> list[int]:
    """Per-step duration in whole minutes. A step's own timers are a HARD
    floor on how long it takes (a 25-minute bake cannot be a 1-minute step);
    a timer-less prep step gets PREP_STEP_MINUTES. This is the concrete,
    model-provided signal — unlike the model's freehand `remaining` values,
    which it fills in by counting down one-per-step and routinely gets wrong
    (real bug found live: 'Italian Cantuccini' claimed 10 min total with
    remaining 10→9→8…→0, while its own timers summed to 55 min of baking/
    cooling/toasting)."""
    costs = []
    for step in steps:
        timer_seconds = sum(
            t.get("seconds", 0)
            for t in (step.get("timers") or [])
            if isinstance(t.get("seconds"), int | float) and not isinstance(t.get("seconds"), bool)
        )
        costs.append(max(1, math.ceil(timer_seconds / 60)) if timer_seconds > 0 else PREP_STEP_MINUTES)
    return costs


def timer_aware_total_minutes(steps: list[dict]) -> int:
    """The recipe's real total, derived from concrete per-step timers rather
    than trusting the model's own total/`remaining` (see _step_costs_minutes
    for why). Used by internal.py's steps-time reconciliation as the honest
    floor a claimed time is checked against."""
    return sum(_step_costs_minutes(steps))


def rewrite_remaining(steps: list[dict]) -> None:
    """Overwrite each step's `remaining` (minutes left AFTER that step) so the
    cook-mode countdown is consistent with the timer-aware total — only
    called when the reconciliation actually corrects the total, so an
    already-honest recipe keeps the model's own values untouched.
    Correct-by-construction against validate_steps: strictly decreasing
    (every cost >= 1) and exactly 0 on the last step."""
    costs = _step_costs_minutes(steps)
    total = sum(costs)
    elapsed = 0
    for step, cost in zip(steps, costs, strict=True):
        elapsed += cost
        step["remaining"] = total - elapsed


def validate_steps(steps: list[dict]) -> None:
    """Part B (2026-07-14) — deterministic backstop for two real bugs found
    by the content-quality audit: steps missing a title entirely (Churros,
    all 5 steps) and escaped-HTML artifacts leaking into stored text
    (&lt;em&gt;socarrat&lt;/em&gt; in Seafood Fideuà — sanitize_step_text
    correctly stripped a disallowed <em> tag but left the escaped entity
    text behind). Runs on the ALREADY-sanitized steps, so it's checking
    exactly what's about to be stored, not the model's raw output."""
    if not steps:
        raise GeneratedStepsInvalid("No steps generated")

    prev_remaining = None
    for i, step in enumerate(steps):
        if not (step.get("title") or "").strip():
            raise GeneratedStepsInvalid(f"Step {i + 1} missing title")

        text = step.get("text") or ""
        if not text.strip():
            raise GeneratedStepsInvalid(f"Step {i + 1} missing text")
        if "&lt;" in text or "&gt;" in text:
            raise GeneratedStepsInvalid(f"Step {i + 1} has an escaped-HTML artifact")
        for tag in _ANY_TAG_PATTERN.findall(text):
            if tag.lower() != "strong":
                raise GeneratedStepsInvalid(f"Step {i + 1} has a disallowed tag <{tag}>")

        remaining = step.get("remaining")
        if remaining is None:
            raise GeneratedStepsInvalid(f"Step {i + 1} missing remaining")
        if prev_remaining is not None and remaining > prev_remaining:
            raise GeneratedStepsInvalid(f"Step {i + 1} remaining increases vs previous step")
        prev_remaining = remaining

        for timer in step.get("timers") or []:
            if not timer.get("label") or not isinstance(timer.get("seconds"), (int, float)):
                raise GeneratedStepsInvalid(f"Step {i + 1} has a malformed timer")
            if timer.get("alertAt") and not timer.get("alertMsg"):
                raise GeneratedStepsInvalid(f"Step {i + 1} has alertAt without alertMsg")

    if steps[-1].get("remaining") != 0:
        raise GeneratedStepsInvalid(f"Last step remaining={steps[-1].get('remaining')!r}, not 0")


STEPS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "submit_steps",
            "description": "Submit the cooking steps for this recipe. Call exactly once to finish.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": (
                                        "Short, descriptive, one line. Group related actions into "
                                        "one step rather than over-splitting."
                                    ),
                                },
                                "text": {
                                    "type": "string",
                                    "description": (
                                        "Detailed instruction: concrete times (never 'a few minutes' "
                                        "or 'until done'), explicit visual/sensory doneness cues, and "
                                        "what to do in parallel if something else is cooking at the "
                                        "same time. Every ingredient this step uses or mentions must "
                                        "be wrapped in <strong>...</strong> — no other HTML tags."
                                    ),
                                },
                                "timers": {
                                    "type": "array",
                                    "description": (
                                        "One entry per timeable action in this step. Omit/empty if "
                                        "nothing in this step needs timing."
                                    ),
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label": {"type": "string"},
                                            "seconds": {"type": "integer"},
                                            "alertAt": {
                                                "type": "integer",
                                                "description": (
                                                    "Optional: seconds remaining at which to show a "
                                                    "mid-timer reminder (e.g. 'flip now'). Omit if "
                                                    "not needed."
                                                ),
                                            },
                                            "alertMsg": {
                                                "type": "string",
                                                "description": "Required if alertAt is set.",
                                            },
                                        },
                                        "required": ["label", "seconds"],
                                    },
                                },
                                "remaining": {
                                    "type": "integer",
                                    "description": (
                                        "Estimated MINUTES LEFT IN THE WHOLE RECIPE after this step "
                                        "finishes (not this step's own duration) — drives a countdown "
                                        "in the UI. Must decrease step over step and be 0 on the last."
                                    ),
                                },
                            },
                            "required": ["title", "text", "remaining"],
                        },
                    }
                },
                "required": ["steps"],
            },
        },
    }
]


async def generate_steps(recipe: dict) -> tuple[list[dict], str]:
    ingredient_names = ", ".join(i["name"] for i in recipe["ingredients"])
    system_prompt = (
        "You are writing detailed, professional-quality cooking steps for a meal-planning app. "
        "Rules, all mandatory:\n"
        "1. Every time reference must be a concrete number (e.g. '4 minutes', '30 seconds') — "
        "never vague phrases like 'a few minutes' or 'until done'.\n"
        "2. Whenever a timed action involves an OBSERVABLE STATE CHANGE (browning, softening, "
        "thickening, turning opaque, etc.), the number must be paired with what that state looks/"
        "smells/sounds/feels like at that point (e.g. '5 minutes, until golden brown', '3 minutes, "
        "until it turns opaque and flakes easily'). The number is a guide, not a guarantee — stove "
        "power, pan type, and ingredient thickness vary, so the visual/sensory cue is the real "
        "signal of doneness, never the number alone. Never give a timed state-change action with a "
        "number and no cue. Purely passive waits with nothing to observe (resting, chilling, "
        "letting a marinade sit) don't need a cue — there's nothing to check by eye, just the "
        "time.\n"
        "3. If one component cooks while another needs attention, say what to do in parallel so "
        "everything finishes together — don't write purely sequential steps for a dish that needs "
        "parallel prep.\n"
        "4. Never reference an ingredient, tool, or prepared component that wasn't introduced in "
        "this step or an earlier one.\n"
        "5. Wrap every ingredient name mentioned in step text in <strong>...</strong> — no other "
        "HTML tags anywhere.\n"
        "6. Keep step titles short and group related actions together — don't over-split into "
        "many tiny steps.\n"
        "7. Add a timer for every genuinely timeable action (simmering, baking, resting, etc.) — "
        "multiple timers in one step if it has multiple timed actions.\n"
        "8. 'remaining' on each step is the estimated minutes left in the WHOLE recipe after that "
        "step finishes, not the step's own duration — it must count down to 0 by the last step. "
        "It MUST account for the real duration of every timer in this and every later step: a "
        "25-minute bake means at least 25 minutes remain going into it, so 'remaining' cannot drop "
        "by just 1 across a step that contains a 25-minute timer. Do NOT simply count down by one "
        "per step — sum the actual timer durations of the remaining steps (plus a little prep time) "
        "and let 'remaining' reflect that."
    )
    user_prompt = (
        f"Title: {recipe['title']}\n"
        f"Description: {recipe['brief_description']}\n"
        f"Ingredients (use exactly these, nothing else): {ingredient_names}\n"
        f"Total estimated time: {recipe.get('time') or 'unspecified'}\n"
        "Generate the cooking steps for this recipe."
    )

    result, provider = await ai_client.run_tool_use_loop(
        system_prompt,
        user_prompt,
        STEPS_TOOLS,
        dispatch={},
        purpose="select_recipe_steps",
        final_tool_name="submit_steps",
        recipe_id=str(recipe["id"]),
    )

    steps = result["steps"]
    for step in steps:
        step["text"] = sanitize_step_text(step.get("text") or "")
    validate_steps(steps)
    return steps, provider
