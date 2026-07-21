"""Pure-logic tests for the JSON-mode tool-call synthesis adapter (no real
provider calls — monkeypatches ai_client._post, the shared low-level seam
every _call_* function routes through) — the piece the new
openrouter_paid/deepinfra waterfall depends on for both models, on both
providers.
"""

import json

import pytest

from app.services import ai_client


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict):
        self.status_code = status_code
        self._json_body = json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_body


def _envelope(content: str | None) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }


@pytest.mark.asyncio
async def test_synthesizes_tool_call_from_json_content(monkeypatch):
    recipe_json = json.dumps({"title": "Test Recipe", "ingredients": []})

    async def fake_post(url, headers, payload, timeout_seconds=30):
        return _FakeResponse(200, _envelope(recipe_json))

    monkeypatch.setattr(ai_client, "_post", fake_post)
    envelope = await ai_client._call_deepinfra(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
        [{"type": "function", "function": {"name": "submit_recipe", "parameters": {}}}],
        model="some-model",
    )
    message = envelope["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["name"] == "submit_recipe"
    assert message["tool_calls"][0]["function"]["arguments"] == recipe_json
    # Downstream parsing must work unmodified on the synthesized call.
    parsed = ai_client._parse_arguments(message["tool_calls"][0]["function"]["arguments"])
    assert parsed == {"title": "Test Recipe", "ingredients": []}


@pytest.mark.asyncio
async def test_empty_content_does_not_synthesize_a_call(monkeypatch):
    async def fake_post(url, headers, payload, timeout_seconds=30):
        return _FakeResponse(200, _envelope(None))

    monkeypatch.setattr(ai_client, "_post", fake_post)
    envelope = await ai_client._call_openrouter_paid(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
        [{"type": "function", "function": {"name": "submit_recipe", "parameters": {}}}],
        model="some-model",
    )
    message = envelope["choices"][0]["message"]
    assert "tool_calls" not in message


@pytest.mark.asyncio
async def test_schema_instruction_appended_once_per_call_not_accumulated(monkeypatch):
    captured_bodies = []

    async def fake_post(url, headers, payload, timeout_seconds=30):
        captured_bodies.append(payload)
        return _FakeResponse(200, _envelope(json.dumps({"x": 1})))

    monkeypatch.setattr(ai_client, "_post", fake_post)
    original_messages = [{"role": "system", "content": "BASE"}, {"role": "user", "content": "go"}]

    await ai_client._call_deepinfra(
        original_messages,
        [{"type": "function", "function": {"name": "submit_recipe", "parameters": {}}}],
        model="m1",
    )
    await ai_client._call_deepinfra(
        original_messages,
        [{"type": "function", "function": {"name": "submit_recipe", "parameters": {}}}],
        model="m2",
    )

    # The ORIGINAL messages list (as run_tool_use_loop owns it) must never be
    # mutated — each call appends the instruction to a fresh copy only.
    assert original_messages[0]["content"] == "BASE"
    for body in captured_bodies:
        system_content = body["messages"][0]["content"]
        assert system_content.count("Respond with ONLY a single JSON object") == 1


@pytest.mark.asyncio
async def test_generic_schema_instruction_used_for_unknown_tool(monkeypatch):
    async def fake_post(url, headers, payload, timeout_seconds=30):
        return _FakeResponse(200, _envelope(json.dumps({"y": 2})))

    monkeypatch.setattr(ai_client, "_post", fake_post)
    envelope = await ai_client._call_deepinfra(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
        [{"type": "function", "function": {"name": "submit_expansion", "parameters": {"type": "object"}}}],
        model="m1",
    )
    message = envelope["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["name"] == "submit_expansion"


@pytest.mark.asyncio
async def test_openrouter_paid_falls_through_phi4_to_mistral_on_failure(monkeypatch, db_pool):
    # Order flipped 2026-07-19 (latency pass): Phi-4 is now first (measured
    # ~2x faster AND the benchmark's validity winner), Mistral is the
    # same-provider fallback.
    call_models = []

    async def fake_post(url, headers, payload, timeout_seconds=30):
        call_models.append(payload["model"])
        if payload["model"] == ai_client.OPENROUTER_PAID_PHI4_MODEL:
            return _FakeResponse(500, {"error": "boom"})
        return _FakeResponse(200, _envelope(json.dumps({"title": "ok"})))

    monkeypatch.setattr(ai_client, "_post", fake_post)
    result, provider = await ai_client.openrouter_paid_completion(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
        [{"type": "function", "function": {"name": "submit_recipe", "parameters": {}}}],
        purpose="test",
    )
    assert provider == "openrouter_paid"
    assert call_models == [ai_client.OPENROUTER_PAID_PHI4_MODEL, ai_client.OPENROUTER_PAID_MISTRAL_MODEL]


@pytest.mark.asyncio
async def test_chat_completion_falls_through_openrouter_paid_to_deepinfra(monkeypatch, db_pool):
    async def fake_post(url, headers, payload, timeout_seconds=30):
        if url == ai_client.OPENROUTER_URL:
            return _FakeResponse(500, {"error": "boom"})
        return _FakeResponse(200, _envelope(json.dumps({"title": "ok"})))

    monkeypatch.setattr(ai_client, "_post", fake_post)
    result, provider = await ai_client.chat_completion(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
        [{"type": "function", "function": {"name": "submit_recipe", "parameters": {}}}],
        purpose="test",
    )
    assert provider == "deepinfra"


@pytest.mark.asyncio
async def test_chat_completion_raises_ai_provider_exhausted_when_both_tiers_fail(monkeypatch, db_pool):
    async def fake_post(url, headers, payload, timeout_seconds=30):
        return _FakeResponse(500, {"error": "boom"})

    monkeypatch.setattr(ai_client, "_post", fake_post)
    with pytest.raises(ai_client.AIProviderExhausted):
        await ai_client.chat_completion(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
            [{"type": "function", "function": {"name": "submit_recipe", "parameters": {}}}],
            purpose="test",
        )
