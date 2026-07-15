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

MAX_TOOL_LOOP_TURNS = 6
MAX_CORRECTIVE_RETRIES = 3

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
# every call for several minutes straight). Unlike OpenRouter's cutoff (skip
# straight to the graceful "chef is busy" message — OpenRouter IS the slow
# fallback already, so a doomed wait there isn't worth it), Groq's window is
# short and refills almost continuously (verified live: token bucket resets
# in ~205ms), so a bounded wait for a slot to free up is usually much
# cheaper than eating OpenRouter's 20-30s congestion tax. This is the queue:
# calls that would burst past the self-imposed per-minute cap wait their
# turn instead of either failing outright or firing anyway.
GROQ_QUEUE_MAX_WAIT_SECONDS = 12
GROQ_QUEUE_POLL_INTERVAL_SECONDS = 2


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
    payload = {"model": model, "messages": messages, "tools": tools}
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


async def _wait_for_groq_capacity() -> bool:
    """Polls the self-imposed per-minute cap (DB-backed, so it's correct
    across concurrent requests/Cloud Run instances, unlike an in-process-only
    limiter) rather than failing the instant it's hit. Returns False if the
    wait budget runs out without capacity freeing up — the caller then falls
    through to the existing OpenRouter fallback exactly as before."""
    waited = 0.0
    while waited < GROQ_QUEUE_MAX_WAIT_SECONDS:
        if await provider_quota.can_call("groq"):
            return True
        await asyncio.sleep(GROQ_QUEUE_POLL_INTERVAL_SECONDS)
        waited += GROQ_QUEUE_POLL_INTERVAL_SECONDS
    return False


async def _call_groq(messages: list[dict], tools: list[dict]) -> dict:
    settings = get_settings()
    resp = await _post(
        GROQ_URL,
        {"Authorization": f"Bearer {settings.groq_api_key}"},
        {"model": GROQ_MODEL, "messages": messages, "tools": tools},
    )
    resp.raise_for_status()
    return resp.json()


async def chat_completion(
    messages: list[dict],
    tools: list[dict],
    purpose: str,
    recipe_id: str | None = None,
    generation_request_id: str | None = None,
) -> tuple[dict, str]:
    """Tries Groq first, then OpenRouter (with its own 2-model fallback) as a
    fully independent second upstream (guardrail: no BYOK routing — see the
    OpenRouter-vs-Groq design discussion). Logs every attempt to
    prompt_audit_log (step 20), whichever provider actually served it.
    recipe_id/generation_request_id give per-recipe, per-request traceability —
    recipe_id is already known for existing-recipe call sites; for brand-new
    recipes generation_request_id lets the caller backfill recipe_id later.

    Returns (response, provider) — added 2026-07-15 after finding every
    caller was hardcoding `source='openrouter'` regardless of which provider
    actually answered (Groq is tried first and usually wins), making
    per-provider quality comparisons impossible from the recipes table.

    Groq is primary (not OpenRouter) — measured live: Groq's llama-3.3-70b
    (LPU hardware, no reasoning trace) answers in ~1s vs. OpenRouter's
    gpt-oss-120b (a reasoning model) taking ~30s for the same request. Groq
    occasionally malforms its own tool-call syntax (a real, seen-live
    reliability quirk), but the caller-level retry loop (fresh_generation.py's
    MAX_GENERATION_ATTEMPTS) now actually retries on that instead of failing
    the whole attempt, making the latency win worth the trade-off.

    Both providers can also be manually disabled via provider_status.py
    (production-readiness Part 4) — deliberately manual-only, never
    auto-flipped by this function itself; see that module's docstring for
    why. groq_only_completion/openrouter_only_completion deliberately do
    NOT check this — they exist specifically to force a provider even when
    diagnosing it (the golden-set test), so gating them the same way would
    make a disabled provider unmeasurable right when you'd want data on it
    most."""
    if await provider_status.is_enabled("groq") and await _wait_for_groq_capacity():
        start = time.monotonic()
        try:
            result = await _call_groq(messages, tools)
            await _log_call(
                "groq", messages, result, start, purpose, None, recipe_id, generation_request_id
            )
            await provider_quota.check_and_alert("groq")
            return result, "groq"
        except Exception as groq_error:
            await _log_call(
                "groq",
                messages,
                None,
                start,
                purpose,
                str(groq_error),
                recipe_id,
                generation_request_id,
            )
    # Either Groq is manually disabled, the call itself failed above, or the
    # queue's wait budget ran out without a slot freeing up — either way,
    # fall through to OpenRouter exactly as before.

    if not await provider_status.is_enabled("openrouter") or not await provider_quota.can_call(
        "openrouter"
    ):
        # Manually disabled or self-imposed cutoff already reached — don't
        # spend a call attempt that's already doomed to fail (or worse,
        # count against the real limit right as it's being hit). Skip
        # straight to the graceful exhaustion path the caller already
        # handles.
        raise AIProviderExhausted("openrouter disabled or self-imposed cutoff reached")

    start = time.monotonic()
    try:
        result = await _call_openrouter(
            messages, tools, fallback_models=[OPENROUTER_FALLBACK_MODEL]
        )
        await _log_call(
            "openrouter", messages, result, start, purpose, None, recipe_id, generation_request_id
        )
        await provider_quota.check_and_alert("openrouter")
        return result, "openrouter"
    except Exception as openrouter_error:
        await _log_call(
            "openrouter",
            messages,
            None,
            start,
            purpose,
            str(openrouter_error),
            recipe_id,
            generation_request_id,
        )
        raise AIProviderExhausted from openrouter_error


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
    if not await _wait_for_groq_capacity():
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
    completion_fn defaults to the Groq-then-OpenRouter chat_completion, but
    the async steps-generation worker passes openrouter_only_completion
    instead (see there for why).

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
    return _normalise_stringified_json(parsed)


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
