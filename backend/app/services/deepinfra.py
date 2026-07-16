import base64

import httpx

from app.core.config import get_settings

URL = "https://api.deepinfra.com/v1/openai/images/generations"
MODEL = "black-forest-labs/FLUX-1-schnell"

EMBEDDINGS_URL = "https://api.deepinfra.com/v1/openai/embeddings"
# Same model name as Cloudflare's @cf/baai/bge-base-en-v1.5 — same 768-dim
# output (verified live), but NOT guaranteed bit-identical across serving
# infra (verified live too: 0.976 cosine similarity between the two
# providers embedding the SAME text, not 1.0). This is why embeddings have
# no automatic cross-provider fallback (see pool_search.py) — mixing
# providers within one pool would introduce exactly that noise right at
# the similarity threshold that matters.
EMBEDDINGS_MODEL = "BAAI/bge-base-en-v1.5"


async def generate_image(prompt: str) -> bytes:
    """Paid PAYG backstop when Cloudflare fails/limits — ~$0.0005/image at
    1024x512 (see cost_status.py for the monthly spend cap governing this).
    Unlike Cloudflare's Flux endpoint, this one accepts an explicit size
    string and returns exactly that, no server-side crop needed.

    Pure API client, no DB access — usage logging lives in image_chain.py,
    which has the recipe_id/user_id context needed for per-recipe traceability
    and is the single call site every caller goes through."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            URL,
            headers={"Authorization": f"Bearer {settings.deepinfra_api_key}"},
            json={"prompt": prompt, "model": MODEL, "size": "1024x512", "n": 1},
        )
        resp.raise_for_status()
        return base64.b64decode(resp.json()["data"][0]["b64_json"])


async def embed_text(text: str) -> list[float]:
    """Same signature as cloudflare.embed_text so every call site is a pure
    import swap (2026-07-16 cutover — see pool_search.py/pool_warmer.py/
    fresh_generation.py/admin.py). Pure API client, no DB access, no
    internal try/except — same convention as generate_image above and as
    cloudflare.embed_text itself; callers decide how to handle a failure."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            EMBEDDINGS_URL,
            headers={"Authorization": f"Bearer {settings.deepinfra_api_key}"},
            json={"model": EMBEDDINGS_MODEL, "input": text},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
