import base64
import json
from io import BytesIO

import httpx
from PIL import Image

from app.core.config import get_settings

CF_BASE = "https://api.cloudflare.com/client/v4/accounts"


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

    Switched to Flux Schnell (from Leonardo Phoenix) originally meaning to
    match the fallback provider's model too — that fallback was Pollinations
    at the time, whose live model roster turned out to be just "sana", not
    Flux, so that consistency goal was never achievable and Pollinations was
    later removed from the chain entirely (see deepinfra.py, the current paid
    backstop, and image_chain.py). Kept Flux here anyway since it's a real,
    confirmed, good-quality Cloudflare model on its own merits. Unlike the
    previous model (Leonardo Phoenix), this endpoint has no width/
    height parameter at all — it returns JSON+base64, not raw bytes, at
    whatever its native size is — so the result is centre-cropped to 2:1
    here to still match the frontend's aspect-ratio:2/1 CSS exactly."""
    settings = get_settings()
    url = f"{CF_BASE}/{settings.cf_account_id}/ai/run/@cf/black-forest-labs/flux-1-schnell"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.cf_api_token}"},
            json={"prompt": prompt, "steps": 4},
        )
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
