"""Raw async OpenRouter chat-completions client for the benchmark — mirrors
deepinfra_client.py's exact public interface (MODELS/USES_JSON_MODE/
chat_completion return shape) so runner.py's _run_one_recipe_case works
unchanged against either provider, just by swapping which client module is
passed in. Built 2026-07-16 to answer a live production question: real
traffic through openrouter_paid_completion (ai_client.py) is seeing a much
higher validation-retry rate than DeepInfra's benchmarked 91.7%/95.8% —
is that the MODEL (Mistral/Phi-4 themselves) or the PROVIDER (OpenRouter's
specific serving of them)? The original benchmark only ever tested these
models via DeepInfra, never via OpenRouter, despite OpenRouter being the
LIVE primary tier — this fills that gap.
"""

import os
import time

import httpx

URL = "https://openrouter.ai/api/v1/chat/completions"

# Same two models production's openrouter_paid_completion tries, in the
# same order — see ai_client.OPENROUTER_PAID_MISTRAL_MODEL/_PHI4_MODEL.
MODELS = {
    "mistral-small-24b": "mistralai/mistral-small-24b-instruct-2501",
    "phi-4": "microsoft/phi-4",
}

# Both confirmed live (this session, FASE T) to reject a bare `tools` param
# on OpenRouter the same way they do on DeepInfra — JSON-mode only.
USES_JSON_MODE: set[str] = {"mistral-small-24b", "phi-4"}

TIMEOUT_SECONDS = 60


class OpenRouterError(Exception):
    pass


async def chat_completion(
    model_key: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.3,
    json_mode: bool = False,
) -> dict:
    """Same normalised return shape as deepinfra_client.chat_completion:
    {ok, latency_ms, raw, error, tokens_in, tokens_out, tool_calls,
    content} — lets extract_tool_call_arguments/extract_json_content
    (imported from deepinfra_client, provider-agnostic) work unchanged."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise OpenRouterError(
            "OPENROUTER_API_KEY not set — export it or add it to backend/.env and `source` it"
        )

    payload = {
        "model": MODELS[model_key],
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    else:
        payload["tools"] = tools
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.post(
                URL, headers={"Authorization": f"Bearer {api_key}"}, json=payload
            )
        latency_ms = int((time.monotonic() - start) * 1000)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "raw": exc.response.text[:2000],
            "error": f"HTTP {exc.response.status_code}",
            "tokens_in": None,
            "tokens_out": None,
            "tool_calls": None,
            "content": None,
        }
    except Exception as exc:  # noqa: BLE001 — recorded as a data point, not raised
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "raw": None,
            "error": repr(exc),
            "tokens_in": None,
            "tokens_out": None,
            "tool_calls": None,
            "content": None,
        }

    message = data["choices"][0]["message"]
    usage = data.get("usage", {})
    return {
        "ok": True,
        "latency_ms": latency_ms,
        "raw": data,
        "error": None,
        "tokens_in": usage.get("prompt_tokens"),
        "tokens_out": usage.get("completion_tokens"),
        "tool_calls": message.get("tool_calls"),
        "content": message.get("content"),
    }
