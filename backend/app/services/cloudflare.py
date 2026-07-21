import base64
import json
from io import BytesIO

import httpx
from PIL import Image

from app.core.config import get_settings

CF_BASE = "https://api.cloudflare.com/client/v4/accounts"


class CloudflareImageQuotaExhausted(Exception):
    """The REAL signal from Cloudflare itself (error code 4006, "you have
    used up your daily free allocation..."), not our own attempt-count
    estimate — raised only when the response actually says so, so callers
    that want to react to genuine exhaustion (pool_warmer, 2026-07-20) don't
    have to guess via a calibrated cap the way cost_status.py's proactive
    check still does for the live-request path."""


class CloudflareTextQuotaExhausted(Exception):
    """Same real 4006 signal as CloudflareImageQuotaExhausted, for the text
    model instead of the image one (2026-07-21) — text and image calls draw
    from the SAME account-wide 10k-Neuron daily pool, so recipe_audit.py's
    ~100+ text calls/run can exhaust it before real users generate any
    images that day if it keeps hammering an already-exhausted Cloudflare
    instead of noticing and switching over. See recipe_audit.py's
    _semantic_findings for how this is used to stop retrying Cloudflare
    mid-run rather than just falling back to DeepInfra row-by-row."""


async def embed_text(text: str) -> list[float]:
    """Embed text with Cloudflare's BGE model (768 dimensions)."""
    settings = get_settings()
    url = f"{CF_BASE}/{settings.cf_account_id}/ai/run/@cf/baai/bge-base-en-v1.5"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.cf_api_token}"},
            json={"text": [text]},
        )
        resp.raise_for_status()
        return resp.json()["result"]["data"][0]


async def generate_text(system_prompt: str, user_prompt: str) -> str:
    """Free Llama text model — used only for speculative pool-warming stubs
    (never for live user requests, which use OpenRouter/Groq). Smaller and
    less capable than Claude Haiku, but that's the right trade for content
    nobody has asked for yet: zero cost regardless of how many stubs never
    get selected, at the price of weaker instruction-following than a frontier
    model — acceptable since every stub gets human-grade text generation
    anyway the moment a real user's search actually surfaces it (step 16)."""
    settings = get_settings()
    url = f"{CF_BASE}/{settings.cf_account_id}/ai/run/@cf/meta/llama-3.1-8b-instruct"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.cf_api_token}"},
            json={
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            },
        )
        if resp.status_code == 429:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            errors = body.get("errors") or []
            if any(e.get("code") == 4006 for e in errors) or "daily free allocation" in resp.text:
                raise CloudflareTextQuotaExhausted(resp.text)
        resp.raise_for_status()
        content = resp.json()["result"]["response"]
        # Cloudflare sometimes auto-parses JSON-shaped output into an object
        # instead of leaving it as a string — normalise so callers always get text.
        return content if isinstance(content, str) else json.dumps(content)


def vector_literal(vector: list[float]) -> str:
    """asyncpg has no native pgvector codec — pass vectors as this string, cast ::vector in SQL."""
    return "[" + ",".join(str(x) for x in vector) + "]"


async def generate_image(prompt: str) -> bytes:
    """First link in the image provider chain. Free, 10K Neurons/day — fails
    cleanly (raises) on limit rather than charging; never attach Workers Paid.

    2026-07-20 — switched from flux-1-schnell to flux-2-klein-9b after a
    same-day model shootout (see backend/benchmarks/ + the model-audit
    artifact): visibly more realistic food photos (no more soft/mushy rice)
    at comparable latency (~2-2.7s measured live vs flux-1-schnell's ~1.8s —
    the gap doesn't matter since image generation always runs in the
    background, never blocking the user-facing response). Also confirmed
    live on DeepInfra (the paid backstop below), so both links in the chain
    now use the same, better model.

    Unlike flux-1-schnell's plain-JSON body, this model's schema requires
    multipart/form-data (confirmed via Cloudflare's own model-schema
    endpoint — a bare JSON POST returns "required properties... 'multipart'"
    with no further explanation). It also has no width/height parameter —
    returns JSON+base64 at its native 1024x1024 — so the result is still
    centre-cropped to 2:1 here, same as before."""
    settings = get_settings()
    url = f"{CF_BASE}/{settings.cf_account_id}/ai/run/@cf/black-forest-labs/flux-2-klein-9b"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.cf_api_token}"},
            files={"prompt": (None, prompt), "steps": (None, "4")},
        )
        if resp.status_code == 429:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            errors = body.get("errors") or []
            if any(e.get("code") == 4006 for e in errors) or "daily free allocation" in resp.text:
                raise CloudflareImageQuotaExhausted(resp.text)
        resp.raise_for_status()
        image_bytes = base64.b64decode(resp.json()["result"]["image"])
        return _crop_to_2_1(image_bytes)


def _crop_to_2_1(image_bytes: bytes) -> bytes:
    """Centre-crop to a 2:1 aspect ratio, whatever the source dimensions are —
    measured at runtime rather than assumed, since Flux Schnell's native
    output size isn't documented by Cloudflare's endpoint."""
    img = Image.open(BytesIO(image_bytes))
    width, height = img.size
    target_height = width / 2
    if height > target_height:
        top = int((height - target_height) / 2)
        img = img.crop((0, top, width, top + int(target_height)))
    elif height < target_height:
        target_width = height * 2
        left = int((width - target_width) / 2)
        img = img.crop((left, 0, left + int(target_width), height))
    out = BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue()
