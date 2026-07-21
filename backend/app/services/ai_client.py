import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core import db
from app.core.config import get_settings
from app.services import provider_quota, provider_status

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_PRIMARY_MODEL = "openai/gpt-oss-120b:free"
OPENROUTER_FALLBACK_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
# Random free-model router, used ONLY by the async steps-generation worker
# (Cloud Tasks) — per the user's explicit split: Groq serves the
# synchronous, user-is-watching suggestions; this serves the deferred,
# nobody's-waiting steps generation, where wildly variable per-call latency
# (measured live: 6.7s to 125s across different underlying models) is fine
# since nothing is blocking on it.
OPENROUTER_FREE_ROUTER_MODEL = "openrouter/free"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# Paid text tier (2026-07-16) — replaces Groq/OpenRouter-free as the live
# waterfall after a same-day benchmark (backend/benchmarks/) found the free
# tiers unreliable under real load. Both models verified live to reject
# real tool-calling on both providers ("Tool calling is not supported" /
# "No endpoints found that support tool use") but work via JSON-mode — see
# _call_json_mode_as_tool_call below.
#
# 2026-07-20 — Mistral bumped 2501 -> 3.2 (2506) after the same-day model
# shootout (see benchmarks/): identical latency and 3/3 contract validity on
# both DeepInfra and OpenRouter, just a newer build. Free upgrade, no
# tradeoff found.
OPENROUTER_PAID_MISTRAL_MODEL = "mistralai/mistral-small-3.2-24b-instruct-2506"
OPENROUTER_PAID_PHI4_MODEL = "microsoft/phi-4"

DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
DEEPINFRA_MISTRAL_MODEL = "mistralai/Mistral-Small-3.2-24B-Instruct-2506"  # casing differs from OpenRouter's slug for the same model
DEEPINFRA_PHI4_MODEL = "microsoft/phi-4"

MAX_TOOL_LOOP_TURNS = 6
MAX_CORRECTIVE_RETRIES = 3

# 2026-07-18 — real bug found live: a batch with pantry items (tabasco,
# mango) and a "pasta" craving came back as "Pasta con Pollo alla Mango e
# Tabasco" — an invented mashup, not a real dish. No temperature was set
# anywhere, so every call ran at each provider's own default (typically
# 0.7-1.0), which is exactly the kind of setting that encourages "creative"
# combination of unrelated hints instead of picking one authentic dish and
# ignoring what doesn't fit. Low but non-zero: 0 would make identical
# inputs always return the identical dish, killing variety across a batch;
# this just discourages invention while still allowing normal variation.
GENERATION_TEMPERATURE = 0.3

# httpx's `timeout=` is an idle/per-chunk timeout, not a total-request
# deadline — a slow-but-still-trickling-data response can run well past it
# (seen live: a single OpenRouter free-model call took 67s despite this
# being set to 45). asyncio.wait_for enforces a real wall-clock cap so a
# slow OpenRouter fails over to Groq quickly instead of stalling the whole
# tool-use loop and risking Cloud Run's request timeout. The async worker
# uses a much longer cap (ASYNC_WORKER_TIMEOUT_SECONDS below) since nobody
# is waiting on it synchronously.
PROVIDER_CALL_TIMEOUT_SECONDS = 30
ASYNC_WORKER_TIMEOUT_SECONDS = 180

# Real bug hit live: parallelizing slot processing in generate_recipes.py (up
# to 3 concurrent meal-type groups) let several Groq calls burst in the same
# instant, tripping Groq's real per-minute token window (429s observed on
# every call for several minutes straight). A bounded wait for a slot to
# free up is usually much cheaper than eating a provider's congestion tax
# outright — this is the queue: calls that would burst past the self-imposed
# per-minute cap wait their turn instead of either failing outright or
# firing anyway. Groq's window is short and refills almost continuously
# (verified live: token bucket resets in ~205ms) — 12s is comfortable.
# OpenRouter gets a shorter budget: with Groq exhausted for a whole day (a
# real, observed case — see provider_quota.is_daily_blocked), OpenRouter
# becomes the de-facto primary for the rest of that day, so a minute-cap
# block there shouldn't instantly fail the slot either, but it's still the
# slower/less reliable provider so the wait stays modest.
CAPACITY_WAIT_POLL_INTERVAL_SECONDS = 2
GROQ_CAPACITY_MAX_WAIT_SECONDS = 12
OPENROUTER_CAPACITY_MAX_WAIT_SECONDS = 15


class AIProviderExhausted(Exception):
    """Both OpenRouter and Groq failed/exhausted, OR the self-imposed
    per-minute/daily cutoff was already reached (see provider_quota.py).
    Step 18 exhaustion behaviour: a live user request must never be queued,
    so this becomes a graceful user-facing message, never a silent retry
    loop — the async worker path is the deliberate exception to "never
    queued", since nothing there is a live, watching user."""


async def _post(
    url: str, headers: dict, payload: dict, timeout_seconds: int = PROVIDER_CALL_TIMEOUT_SECONDS
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=max(45, timeout_seconds + 15)) as client:
        try:
            return await asyncio.wait_for(
                client.post(url, headers=headers, json=payload),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            raise TimeoutError(f"provider call exceeded {timeout_seconds}s") from exc


async def _call_openrouter(
    messages: list[dict],
    tools: list[dict],
    model: str = OPENROUTER_PRIMARY_MODEL,
    fallback_models: list[str] | None = None,
    timeout_seconds: int = PROVIDER_CALL_TIMEOUT_SECONDS,
) -> dict:
    settings = get_settings()
    payload = {"model": model, "messages": messages, "tools": tools, "temperature": GENERATION_TEMPERATURE}
    if fallback_models:
        payload["models"] = [model, *fallback_models]
    resp = await _post(
        OPENROUTER_URL,
        {"Authorization": f"Bearer {settings.openrouter_api_key}"},
        payload,
        timeout_seconds=timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()


async def _wait_for_capacity(provider: str, max_wait_s: float) -> bool:
    """Polls the self-imposed per-minute cap (DB-backed, so it's correct
    across concurrent requests/Cloud Run instances, unlike an in-process-only
    limiter) rather than failing the instant it's hit. Fast-paths out with NO
    wait at all when a DAILY cap is the blocker — polling can't help there
    (see provider_quota.is_daily_blocked's docstring for the real cost this
    avoids). Returns False if the wait budget runs out (or the daily
    fast-path fired) without capacity freeing up — the caller then falls
    through to its next provider exactly as before."""
    if await provider_quota.can_call(provider):
        return True
    if await provider_quota.is_daily_blocked(provider):
        return False
    waited = 0.0
    while waited < max_wait_s:
        await asyncio.sleep(CAPACITY_WAIT_POLL_INTERVAL_SECONDS)
        waited += CAPACITY_WAIT_POLL_INTERVAL_SECONDS
        if await provider_quota.can_call(provider):
            return True
    return False


async def _call_groq(messages: list[dict], tools: list[dict]) -> dict:
    settings = get_settings()
    resp = await _post(
        GROQ_URL,
        {"Authorization": f"Bearer {settings.groq_api_key}"},
        {"model": GROQ_MODEL, "messages": messages, "tools": tools, "temperature": GENERATION_TEMPERATURE},
    )
    resp.raise_for_status()
    return resp.json()


# Hand-authored JSON-mode schema text, ported from backend/benchmarks/
# mesa_contract.py where it was already live-verified (91.7%/100% valid on
# mesa's real contract) — not reimplemented from scratch. Only submit_recipe
# and submit_steps have a hand-authored version; anything else falls back
# to _generic_schema_instruction below.
_JSON_MODE_RECIPE_INSTRUCTION = (
    "\n\nRespond with ONLY a single JSON object, no markdown code fences, no text before or after it. "
    'The object must have exactly this shape: {"title": string, "brief_description": string, '
    '"cuisine": string, "main_protein": string, "image_prompt": string, "time": string, '
    '"kcal": integer, "ingredients": [{"name": string, "qty": number, "unit": string, '
    '"scaling": "linear"|"seasoning"|"heat"|"fixed", "tier": "mandatory"|"recommended"|"optional", '
    '"allergen_tags": [string]}], '
    '"variations": [{"name": string, "add": [{same shape as an ingredient above}], '
    '"remove": [string], "kcal": integer (optional), "time": string (optional), '
    '"image_prompt": string (optional)}]}. '
    "Actively look for variations — most toast/yogurt/egg breakfasts and many mains DO have at least "
    "one natural variant real people actually make; don't skip this by default. Never an invented/"
    "random combination. Topping/mix-in swaps for a breakfast (different nuts/fruit on "
    "yogurt, different toast toppings), or a genuine accompaniment choice for a lunch/dinner (e.g. a "
    "stir-fry or curry served with rice vs noodles vs flatbread) — 0 to 3 items, each a small DELTA "
    "from the base recipe (add ingredients, or remove base ingredient NAMES), never a different dish. "
    "When the base is a main-protein dish that is traditionally eaten WITH a starch or side but "
    "doesn't inherently include one (chicken satay, grilled fish or chicken, kebab, grilled meats), "
    "offering side/accompaniment variations (steamed rice, rice noodles, flatbread, a simple salad) "
    "is exactly the kind of natural variation to include — those dishes are rarely eaten bare. "
    "Each variation must itself be something people genuinely, commonly make this way — apply the "
    "same authenticity bar as the base dish itself, not a creative liberty. When a well-known variant "
    "adds a whole protein/garnish on top of a plainer classic dish (e.g. gazpacho with shrimp, "
    "carbonara with peas), that variant belongs HERE, in variations — never as the base recipe itself; "
    "the base recipe must always be the classic/traditional form. Omit or use an empty list whenever "
    "the recipe genuinely has no natural variations — never invent fake ones just to fill the list. "
    "Each variation's optional \"kcal\" is the TOTAL kcal for the WHOLE recipe when made with that "
    "variation instead of the base (same computation method as the top-level kcal) — include it "
    "whenever the variation meaningfully changes calories (e.g. adding a starchy side), omit it if "
    "calorie-neutral. Optional \"time\" is the TOTAL time for the whole recipe when made with that "
    "variation, same format as the top-level time — include it only if the variation genuinely adds "
    "or removes real prep/cook time (e.g. grilling shrimp), omit it if the time doesn't meaningfully "
    "change. Optional \"image_prompt\" follows the exact same rules as the top-level image_prompt "
    "below (finished-plated-dish description, same ending) — include it ONLY when the variation would "
    "look visibly different in a photo (added a whole new visible ingredient), omit it for changes "
    "that wouldn't show (e.g. no cilantro, less salt) — the base recipe's own photo is reused for those. "
    "qty must be a JSON number, never a string. unit must be metric only (g, kg, ml, l, tsp, tbsp, "
    "or a plain count like whole/clove/slice/piece) — never imperial. "
    "title/brief_description/ingredient names/steps must be written in English even if the user's "
    "own notes or pantry items were written in a different language — translate any foreign-"
    "language ingredient into its English name. The TITLE must be in English too, with NO exception "
    "for 'this is the dish's real name' — translate/describe it in English instead (bad: 'Frutta e "
    "Yogurt', 'Chocolate con Churros' — these are just literal foreign words for 'Fruit and Yogurt', "
    "'Chocolate with Churros', not special names; good: 'Fruit and Yogurt Bowl', 'Churros with "
    "Chocolate Sauce'). The only thing allowed to stay non-English is a single word that's already a "
    "standard English loanword for an internationally-known dish (e.g. 'Paella', 'Risotto', 'Pad "
    "Thai', 'Sushi', 'Quesadilla') — never a whole foreign-language phrase. Its ingredients/"
    "description/steps are still written in English regardless. Never include the words "
    "'breakfast', 'lunch', or 'dinner' in the title itself (bad: 'Asian-Style Breakfast Fried "
    "Noodles') — the same dish can genuinely be served at a different meal type later, and a "
    "meal-type word baked into the name reads as wrong once that happens. Name the dish itself. "
    "image_prompt must describe how the FINISHED PLATED DISH visually looks — colours, textures, "
    "garnish, vessel, style — never just the dish name or a one-line ingredient list (bad: 'Beef "
    "Quesadillas'; good: 'Two golden crispy quesadilla wedges on a wooden board, melted cheddar and "
    "seasoned beef visible at the cut edge, chunky guacamole and sour cream on the side'). It must "
    "genuinely depict what makes THIS specific dish visually recognizable as itself, not a generic "
    "plate of its main ingredient. If the dish's name is a 'false friend' that commonly means a "
    "DIFFERENT food in English/international usage (e.g. Spanish tortilla is an egg-and-potato "
    "omelette, not a Mexican flour tortilla wrap or flatbread), explicitly rule out the wrong "
    "reading in the prompt itself (e.g. 'a thick Spanish potato omelette, sliced into wedges — an "
    "egg dish, not a flatbread or wrap') — the image model has no other way to know which one you "
    "mean. End it with exactly ', close-up food photography, warm natural light, appetizing'."
)
_JSON_MODE_STEPS_INSTRUCTION = (
    "\n\nRespond with ONLY a single JSON object, no markdown code fences, no text before or after "
    'it, shaped exactly like this: {"steps": [{"title": string, "text": string, "timers": '
    '[{"label": string, "seconds": integer, "alertAt": integer (optional), "alertMsg": string '
    '(required if alertAt set)}], "remaining": integer}]}. "remaining" must strictly decrease '
    "step over step and be exactly 0 on the last step."
)
_JSON_MODE_INSTRUCTIONS = {
    "submit_recipe": _JSON_MODE_RECIPE_INSTRUCTION,
    "submit_steps": _JSON_MODE_STEPS_INSTRUCTION,
}


def _generic_schema_instruction(tool: dict) -> str:
    """Fallback for a tool with no hand-authored instruction above
    (submit_expansion, submit_translation) — dumps the tool's own JSON
    Schema as text; its `description` fields already carry the real
    guidance, this just conveys the shape since JSON mode has no `tools`
    parameter to carry it structurally."""
    params = tool["function"]["parameters"]
    return (
        "\n\nRespond with ONLY a single JSON object, no markdown code fences, no text before or "
        f"after it, matching this JSON Schema: {json.dumps(params)}"
    )


async def _call_json_mode_as_tool_call(
    url: str,
    headers: dict,
    messages: list[dict],
    tools: list[dict],
    model: str,
    timeout_seconds: int = PROVIDER_CALL_TIMEOUT_SECONDS,
) -> dict:
    """Shared by _call_openrouter_paid/_call_deepinfra, for models that
    reject `tools` but accept response_format=json_object. Every real
    caller in this codebase passes exactly one tool schema (confirmed live
    2026-07-16), so "the tool" the model must produce is unambiguous even
    without a real tools parameter. Synthesizes the SAME message.tool_calls
    shape a real tool-calling provider would return, with `arguments` left
    as the RAW, unparsed content string — never pre-parsed here — so
    run_tool_use_loop and the existing _parse_arguments/
    _normalise_stringified_json (top-level-array recovery, nested-
    stringified-JSON recovery) run on a synthetic call exactly as they
    would on a real one, with zero duplicated logic and zero changes
    needed above this function."""
    tool_name = tools[0]["function"]["name"]
    instruction = _JSON_MODE_INSTRUCTIONS.get(tool_name) or _generic_schema_instruction(tools[0])
    # Copy, don't mutate — completion_fn is called once per turn with a
    # growing `messages` list owned by run_tool_use_loop; appending to a
    # copy each time means the instruction is added exactly once per real
    # HTTP call, never accumulating across turns.
    json_messages = [dict(m) for m in messages]
    json_messages[0] = {**json_messages[0], "content": json_messages[0]["content"] + instruction}
    payload = {
        "model": model,
        "messages": json_messages,
        "response_format": {"type": "json_object"},
        "temperature": GENERATION_TEMPERATURE,
    }
    resp = await _post(url, headers, payload, timeout_seconds=timeout_seconds)
    resp.raise_for_status()
    envelope = resp.json()
    message = envelope["choices"][0]["message"]
    content = message.get("content")
    if content:
        # Only synthesize a tool call if the model actually produced
        # something — empty/missing content correctly falls through to
        # run_tool_use_loop's existing "no tool_calls -> corrective retry"
        # path instead of a synthetic call with garbage arguments.
        message["tool_calls"] = [
            {
                "id": "json_mode_call_0",
                "type": "function",
                "function": {"name": tool_name, "arguments": content},
            }
        ]
    return envelope


async def _call_openrouter_paid(messages: list[dict], tools: list[dict], model: str) -> dict:
    settings = get_settings()
    return await _call_json_mode_as_tool_call(
        OPENROUTER_URL, {"Authorization": f"Bearer {settings.openrouter_api_key}"}, messages, tools, model
    )


async def _call_deepinfra(messages: list[dict], tools: list[dict], model: str) -> dict:
    settings = get_settings()
    return await _call_json_mode_as_tool_call(
        DEEPINFRA_URL, {"Authorization": f"Bearer {settings.deepinfra_api_key}"}, messages, tools, model
    )


async def openrouter_paid_completion(
    messages: list[dict],
    tools: list[dict],
    purpose: str,
    recipe_id: str | None = None,
    generation_request_id: str | None = None,
) -> tuple[dict, str]:
    """Paid OpenRouter tier — Phi-4 then Mistral Small, both via the
    JSON-mode adapter (neither supports real tool-calling on OpenRouter,
    verified live). Tries both models explicitly here (not OpenRouter's own
    `models:` array fallback used by the old free-tier path) so every
    attempt gets its own prompt_audit_log row on both providers equally,
    instead of one opaque multi-model call. No capacity pre-check: OpenRouter
    enforces its own $ balance in real-time (confirmed live via a real 403
    once the account's credit limit was hit) and there's no free-tier
    pacing concern for a metered provider."""
    if not await provider_status.is_enabled("openrouter_paid"):
        raise AIProviderExhausted("openrouter_paid disabled")

    last_error: Exception | None = None
    # Phi-4 FIRST (2026-07-19, latency pass): measured live at 8.1-8.5s per
    # recipe vs Mistral's 17.4s average on the identical contract — AND it
    # was already the benchmark's validity winner (95.8% vs 91.7%, see
    # benchmarks/results/). The original Mistral-first order carried no
    # data-backed rationale; this one does. Mistral stays as the same-
    # provider fallback.
    for model in (OPENROUTER_PAID_PHI4_MODEL, OPENROUTER_PAID_MISTRAL_MODEL):
        start = time.monotonic()
        try:
            result = await _call_openrouter_paid(messages, tools, model=model)
            await _log_call(
                "openrouter_paid", messages, result, start, purpose, None, recipe_id, generation_request_id
            )
            return result, "openrouter_paid"
        except Exception as exc:
            last_error = exc
            await _log_call(
                "openrouter_paid", messages, None, start, purpose, str(exc), recipe_id, generation_request_id
            )
    raise AIProviderExhausted("openrouter_paid: both models failed") from last_error


async def deepinfra_completion(
    messages: list[dict],
    tools: list[dict],
    purpose: str,
    recipe_id: str | None = None,
    generation_request_id: str | None = None,
) -> tuple[dict, str]:
    """DeepInfra tier — same 2 models, same JSON-mode adapter, same
    explicit-per-model-attempt logging as openrouter_paid_completion. No
    free tier, pure pay-per-use, so no capacity pre-check here either."""
    if not await provider_status.is_enabled("deepinfra"):
        raise AIProviderExhausted("deepinfra disabled")

    last_error: Exception | None = None
    # Same Phi-4-first ordering as openrouter_paid_completion above, same
    # rationale (measured 10.8-11.5s vs 15.8-23s here on DeepInfra).
    for model in (DEEPINFRA_PHI4_MODEL, DEEPINFRA_MISTRAL_MODEL):
        start = time.monotonic()
        try:
            result = await _call_deepinfra(messages, tools, model=model)
            await _log_call(
                "deepinfra", messages, result, start, purpose, None, recipe_id, generation_request_id
            )
            return result, "deepinfra"
        except Exception as exc:
            last_error = exc
            await _log_call(
                "deepinfra", messages, None, start, purpose, str(exc), recipe_id, generation_request_id
            )
    raise AIProviderExhausted("deepinfra: both models failed") from last_error


async def chat_completion(
    messages: list[dict],
    tools: list[dict],
    purpose: str,
    recipe_id: str | None = None,
    generation_request_id: str | None = None,
) -> tuple[dict, str]:
    """Tries paid OpenRouter (Phi-4, then Mistral Small) first, then
    DeepInfra (same 2 models) — replaces the Groq/OpenRouter-free waterfall
    (2026-07-16): a same-day benchmark (backend/benchmarks/) found both free
    tiers unreliable under real concurrent load (Groq hit its own daily
    token cap from testing, OpenRouter free-tier had a 90%+ error rate the
    same day), while Mistral Small 24B/Phi-4 scored 91.7%/95.8% valid on
    mesa's real contract across two independently-verified paid providers.
    Phi-4 goes first within each provider (2026-07-19 latency pass: ~2x
    faster, also the validity winner). OpenRouter goes first since there's a
    real prepaid credit balance to spend down before DeepInfra becomes pure
    metered billing.

    _call_groq/_call_openrouter/groq_only_completion/openrouter_only_completion
    are unchanged and still used directly by the golden-set quality test —
    this function just no longer calls them, so free-tier capability stays
    available for diagnosis without being live traffic's fallback.

    If both new tiers fail, this raises AIProviderExhausted exactly as
    before — the existing graceful "chef is busy" user-facing path needs no
    changes."""
    try:
        return await openrouter_paid_completion(messages, tools, purpose, recipe_id, generation_request_id)
    except AIProviderExhausted:
        return await deepinfra_completion(messages, tools, purpose, recipe_id, generation_request_id)


async def openrouter_only_completion(
    messages: list[dict],
    tools: list[dict],
    purpose: str,
    recipe_id: str | None = None,
    generation_request_id: str | None = None,
) -> tuple[dict, str]:
    """Used ONLY by the async steps-generation worker (Cloud Tasks) — see
    OPENROUTER_FREE_ROUTER_MODEL above. Deliberately skips Groq entirely
    (unlike chat_completion): the user's explicit design keeps these two
    paths on separate providers. Same signature shape as chat_completion so
    run_tool_use_loop can use either interchangeably via completion_fn."""
    if not await provider_quota.can_call("openrouter"):
        raise AIProviderExhausted("openrouter self-imposed cutoff reached")

    start = time.monotonic()
    try:
        result = await _call_openrouter(
            messages,
            tools,
            model=OPENROUTER_FREE_ROUTER_MODEL,
            timeout_seconds=ASYNC_WORKER_TIMEOUT_SECONDS,
        )
        await _log_call("openrouter", messages, result, start, purpose, None, recipe_id, None)
        await provider_quota.check_and_alert("openrouter")
        return result, "openrouter"
    except Exception as exc:
        await _log_call("openrouter", messages, None, start, purpose, str(exc), recipe_id, None)
        raise AIProviderExhausted from exc


async def groq_only_completion(
    messages: list[dict],
    tools: list[dict],
    purpose: str,
    recipe_id: str | None = None,
    generation_request_id: str | None = None,
) -> tuple[dict, str]:
    """Forces Groq only, bypassing chat_completion's waterfall entirely —
    added 2026-07-15 for the paired golden-set quality test (production-
    readiness Part 3). chat_completion's organic traffic can't fairly
    compare providers: OpenRouter only ever sees the residual requests Groq
    already failed on, not a random sample of the same distribution. This
    plus openrouter_only_completion, called on the SAME request matrix, is
    how the comparison becomes fair. Mirrors openrouter_only_completion's
    shape exactly so both plug into run_tool_use_loop's completion_fn."""
    if not await _wait_for_capacity("groq", GROQ_CAPACITY_MAX_WAIT_SECONDS):
        raise AIProviderExhausted("groq self-imposed cutoff reached / capacity unavailable")

    start = time.monotonic()
    try:
        result = await _call_groq(messages, tools)
        await _log_call(
            "groq", messages, result, start, purpose, None, recipe_id, generation_request_id
        )
        await provider_quota.check_and_alert("groq")
        return result, "groq"
    except Exception as exc:
        await _log_call(
            "groq", messages, None, start, purpose, str(exc), recipe_id, generation_request_id
        )
        raise AIProviderExhausted from exc


async def _log_call(
    provider: str,
    messages: list[dict],
    result: dict | None,
    start: float,
    purpose: str,
    error: str | None,
    recipe_id: str | None,
    generation_request_id: str | None,
) -> None:
    latency_ms = int((time.monotonic() - start) * 1000)
    message = result["choices"][0]["message"] if result else None
    await db.pool().execute(
        """
        INSERT INTO prompt_audit_log
            (model, prompt_text, completion_text, tool_calls, latency_ms, tokens_in, tokens_out, error, recipe_id, generation_request_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """,
        f"{provider}:{purpose}",
        messages[-1]["content"] if messages else None,
        message.get("content") if message else None,
        _tool_calls_native(message),
        latency_ms,
        (result or {}).get("usage", {}).get("prompt_tokens"),
        (result or {}).get("usage", {}).get("completion_tokens"),
        error,
        recipe_id,
        generation_request_id,
    )


def _tool_calls_native(message: dict | None) -> list | None:
    if not message or not message.get("tool_calls"):
        return None
    return message["tool_calls"]


def _normalise_assistant_message(message: dict) -> dict:
    """Strip provider-specific extensions (e.g. OpenRouter reasoning models
    attach their own `reasoning`/`reasoning_details` fields) before this
    message re-enters the shared conversation history. A real bug seen
    live: Groq's stricter schema validation 400s on an unrecognized
    `reasoning_details` field when it receives OpenRouter's own assistant
    message back mid-loop, since both providers share the same history —
    keeping only the standard fields makes the history portable across
    whichever provider ends up serving the next turn."""
    normalised: dict = {"role": message.get("role", "assistant"), "content": message.get("content")}
    if message.get("tool_calls"):
        normalised["tool_calls"] = message["tool_calls"]
    return normalised


ToolDispatch = dict[str, Callable[..., Awaitable[Any]]]


async def run_tool_use_loop(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    dispatch: ToolDispatch,
    purpose: str,
    final_tool_name: str = "submit_recipe",
    recipe_id: str | None = None,
    generation_request_id: str | None = None,
    completion_fn: Callable[..., Awaitable[tuple[dict, str]]] = chat_completion,
) -> tuple[dict, str]:
    """Multi-turn tool-use loop. Ends when the model calls `final_tool_name`.
    Guardrail 19(b): a bad/unknown tool call gets a corrective tool_result
    instead of crashing, up to MAX_CORRECTIVE_RETRIES before giving up.
    completion_fn defaults to chat_completion (the paid OpenRouter-then-
    DeepInfra waterfall) — every live call site uses this default now
    (2026-07-20: stub_expansion.py's Groq override was dropped along with
    the nightly pool_warmer job that was its only reason to use a free
    tier). groq_only_completion/openrouter_only_completion still exist for
    the golden-set quality test only, never passed here in production.

    Returns (parsed_arguments, provider) — provider is whichever one
    answered the LAST turn (the one that actually produced final_tool_name);
    a multi-turn conversation could in theory span providers if an earlier
    turn's provider later dropped below quota mid-loop, so this always
    reflects the true source of the final answer, not just the first turn."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    corrective_retries = 0
    last_provider = "unknown"

    for _ in range(MAX_TOOL_LOOP_TURNS):
        response, last_provider = await completion_fn(
            messages,
            tools,
            purpose,
            recipe_id=recipe_id,
            generation_request_id=generation_request_id,
        )
        message = response["choices"][0]["message"]
        messages.append(_normalise_assistant_message(message))

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            corrective_retries += 1
            if corrective_retries > MAX_CORRECTIVE_RETRIES:
                raise AIProviderExhausted
            messages.append(
                {
                    "role": "user",
                    "content": f"You must call {final_tool_name} with the final result to finish.",
                }
            )
            continue

        for call in tool_calls:
            name = call["function"]["name"]
            if name == final_tool_name:
                try:
                    return _parse_arguments(call["function"]["arguments"]), last_provider
                except (json.JSONDecodeError, TypeError) as exc:
                    # Real bug found live (2026-07-15, via the golden-set
                    # quality test): a malformed FINAL answer used to crash
                    # this whole loop uncaught, past every caller's own
                    # retry logic (fresh_generation.py's MAX_GENERATION_
                    # ATTEMPTS never got a chance to run, since this wasn't
                    # AIProviderExhausted). Same corrective-retry treatment
                    # as a malformed intermediate tool call below, not a
                    # special case — cheaper than aborting the whole attempt,
                    # since the model gets a chance to self-correct within
                    # this same conversation instead of starting over cold.
                    messages.append(_corrective_tool_result(call, f"Malformed arguments: {exc}"))
                    corrective_retries += 1
                    continue

            if name not in dispatch:
                messages.append(_corrective_tool_result(call, f"Unknown tool '{name}'"))
                corrective_retries += 1
                continue

            try:
                args = _parse_arguments(call["function"]["arguments"])
                result = await dispatch[name](**args)
                messages.append(_tool_result(call, result))
            except Exception as exc:  # noqa: BLE001 — corrective path, not a crash
                messages.append(_corrective_tool_result(call, str(exc)))
                corrective_retries += 1

        if corrective_retries > MAX_CORRECTIVE_RETRIES:
            raise AIProviderExhausted

    raise AIProviderExhausted


def _parse_arguments(raw: str | dict) -> dict:
    parsed = raw if isinstance(raw, dict) else json.loads(raw)
    normalised = _normalise_stringified_json(parsed)
    if not isinstance(normalised, dict):
        # Found live via the DeepInfra benchmark (2026-07-15): a model can
        # wrap its tool-call arguments in a top-level JSON array instead of
        # an object (e.g. `[{...}]`) — valid JSON, wrong shape. Every caller
        # assumes a dict (recipe["ingredients"], .get(), etc.), so this used
        # to silently hand back a list and crash deep in unrelated code
        # instead of at this single, already-guarded boundary. Raising
        # TypeError here (not a new exception type) means this plugs into
        # the SAME corrective-retry handling both call sites already have
        # for _parse_arguments — intermediate tool calls (broad except) and
        # the final-answer branch (explicit `except (json.JSONDecodeError,
        # TypeError)`, added for the sibling malformed-JSON bug).
        raise TypeError(f"expected a JSON object, got {type(normalised).__name__}")
    return normalised


def _normalise_stringified_json(value: Any) -> Any:
    """Guardrail 19(b) — some models (seen live: Groq's fallback) put a nested
    array/object into a tool-call argument as a JSON-encoded string, sometimes
    with trailing garbage after it, instead of a real nested structure. Recover
    the leading valid JSON value rather than silently persisting a string like
    '[{"title": ...}]\\n}' into a jsonb column that expects an actual array."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "[{":
            try:
                parsed, _ = json.JSONDecoder().raw_decode(stripped)
                return _normalise_stringified_json(parsed)
            except json.JSONDecodeError:
                return value
        return value
    if isinstance(value, list):
        return [_normalise_stringified_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalise_stringified_json(v) for k, v in value.items()}
    return value


def _tool_result(call: dict, result: Any) -> dict:
    return {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}


def _corrective_tool_result(call: dict, error: str) -> dict:
    return {"role": "tool", "tool_call_id": call["id"], "content": json.dumps({"error": error})}
