import html
import re

from app.services import ai_client

# step.text is rendered as raw HTML by the frontend (to support <strong>
# ingredient highlighting) — escape everything else so a prompt injection or
# plain hallucination can never inject a real tag (script, img/onerror, etc.)
# into the page. Same "don't trust the model" philosophy as the allergen
# safety net: sanitize the model's own output, not just its inputs.
_ALLOWED_TAG_PATTERN = re.compile(r"&lt;(/?)strong&gt;", re.IGNORECASE)


def sanitize_step_text(text: str) -> str:
    escaped = html.escape(text, quote=False)
    return _ALLOWED_TAG_PATTERN.sub(lambda m: f"<{m.group(1)}strong>", escaped)


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


async def generate_steps(recipe: dict) -> list[dict]:
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
        "step finishes, not the step's own duration — it must count down to 0 by the last step."
    )
    user_prompt = (
        f"Title: {recipe['title']}\n"
        f"Description: {recipe['brief_description']}\n"
        f"Ingredients (use exactly these, nothing else): {ingredient_names}\n"
        f"Total estimated time: {recipe.get('time') or 'unspecified'}\n"
        "Generate the cooking steps for this recipe."
    )

    result = await ai_client.run_tool_use_loop(
        system_prompt,
        user_prompt,
        STEPS_TOOLS,
        dispatch={},
        purpose="select_recipe_steps",
        final_tool_name="submit_steps",
        recipe_id=str(recipe["id"]),
        completion_fn=ai_client.openrouter_only_completion,
    )

    steps = result["steps"]
    for step in steps:
        step["text"] = sanitize_step_text(step.get("text") or "")
    return steps
