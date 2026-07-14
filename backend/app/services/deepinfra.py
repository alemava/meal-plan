import base64

import httpx

from app.core.config import get_settings

URL = "https://api.deepinfra.com/v1/openai/images/generations"
MODEL = "black-forest-labs/FLUX-1-schnell"


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
