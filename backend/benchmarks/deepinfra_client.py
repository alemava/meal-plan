"""Raw async DeepInfra chat-completions client for the benchmark — separate
from app/services/deepinfra.py (that one is the production image client,
different endpoint/shape entirely). Mirrors app/services/ai_client.py's
_call_groq/_call_openrouter pattern (OpenAI-compatible tool-calling), since
that's the exact call shape production would use if DeepInfra becomes a
real provider (see CLAUDE.md's FASE W, not built yet — this benchmark is
what decides whether it's worth building).
"""

import json
import os
import time

import httpx

URL = "https://api.deepinfra.com/v1/openai/chat/completions"

MODELS = {
    "llama-3.1-8b": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
    "mistral-small-24b": "mistralai/Mistral-Small-24B-Instruct-2501",
    "gemma-3-27b": "google/gemma-3-27b-it",
}

# Verified live against DeepInfra's own model pages, 2026-07-15 — kept here
# (not hardcoded in the report) so a real price change only needs updating
# in one place. $ per 1M tokens.
PRICES_PER_1M = {
    "llama-3.1-8b": {"input": 0.02, "output": 0.03},
    "mistral-small-24b": {"input": 0.05, "output": 0.08},
    "gemma-3-27b": {"input": 0.08, "output": 0.16},
}

# Found live in R0 (2026-07-15), NOT what DeepInfra's own model page claims
# ("native function calling and JSON outputting"): a real tools-bearing
# request against this exact model returns a clean HTTP 405 —
# "Tool calling is not supported for model: mistralai/Mistral-Small-24B-
# Instruct-2501" — verified via a raw curl, not a fluke of this harness.
# Every other model here uses real structured tool-calling; this one is
# tested via a JSON-mode adapter path instead (response_format=json_object
# + the schema spelled out in the prompt), same as mesa's real architecture
# would need to if this model were ever chosen.
USES_JSON_MODE: set[str] = {"mistral-small-24b"}

TIMEOUT_SECONDS = 60


class DeepInfraError(Exception):
    pass


async def chat_completion(
    model_key: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.3,
    json_mode: bool = False,
) -> dict:
    """Returns a normalised result dict: {ok, latency_ms, raw, error,
    tokens_in, tokens_out, tool_calls, content}. Never raises for a normal
    HTTP/API failure — that's a benchmark DATA POINT (reliability), not a
    bug in the harness. Only raises DeepInfraError for a setup problem
    (missing API key), which should stop the whole run.

    json_mode=True sends response_format=json_object instead of tools — for
    USES_JSON_MODE models, whose caller is expected to have already spelled
    out the target schema in the prompt itself (tools is ignored in this
    mode, DeepInfra rejects the two combined the same way it rejects tools
    alone for these models)."""
    api_key = os.environ.get("DEEPINFRA_API_KEY", "")
    if not api_key:
        raise DeepInfraError(
            "DEEPINFRA_API_KEY not set — export it or add it to backend/.env and `source` it"
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


def estimated_cost_usd(model_key: str, tokens_in: int, tokens_out: int) -> float:
    prices = PRICES_PER_1M[model_key]
    return (tokens_in / 1_000_000) * prices["input"] + (tokens_out / 1_000_000) * prices["output"]


def extract_tool_call_arguments(result: dict, tool_name: str) -> dict | None:
    """None if the model didn't call the expected tool at all, OR if it did
    but the arguments aren't valid JSON (found live: Gemma returned a
    truncated/unterminated JSON string in one case) or aren't a JSON object
    once parsed (found live: Llama sometimes wraps its answer in a
    top-level array, `[{...}]` instead of `{...}`) — production's own
    ai_client._parse_arguments already treats both as the same class of
    problem (a TypeError/JSONDecodeError the caller's corrective-retry
    catches); this harness has no retry loop to lean on, so it just records
    None, a real reliability finding, not an error to swallow."""
    for call in result.get("tool_calls") or []:
        if call["function"]["name"] == tool_name:
            raw = call["function"]["arguments"]
            if isinstance(raw, dict):
                return raw
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def extract_json_content(result: dict) -> dict | None:
    """JSON-mode counterpart to extract_tool_call_arguments — None if the
    content isn't parseable JSON at all (a real reliability finding for a
    model tested via this path, not an error to swallow)."""
    content = result.get("content")
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
