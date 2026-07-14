"""ADR 4 — nightly pool warming. Generates a handful of speculative 'stub'
recipes (title + description + image, no ingredients/steps yet) using only
free Cloudflare models, so real users' hybrid searches (step 15) have more
to find. Stubs only cost real AI-provider usage when step 16 expands one on
demand — this script itself is zero-Anthropic/OpenRouter-cost by design.

Triggered nightly by Cloud Scheduler hitting POST /api/admin/pool-warm (see
app/api/admin.py). Can also run standalone for local testing:
    python pool_warmer.py
"""

import json
import random

from app.core import db
from app.services import cloudflare, image_chain

STUBS_PER_NIGHT = 8
DEFAULT_CUISINES = ["italian", "spanish", "asian"]

SYSTEM_PROMPT = (
    "You invent simple, appealing dinner recipe ideas for a meal-planning app. "
    "Respond with ONLY a JSON object: "
    '{"title": "...", "brief_description": "...", "main_protein": "...", "image_prompt": "..."}. '
    "image_prompt should describe the finished plated dish visually (colours, textures, "
    "garnish, vessel) for an image generator, ending with "
    '", close-up food photography, warm natural light, appetizing".'
)


async def _popular_cuisines() -> list[str]:
    """Step 32 — bias stub generation toward cuisines real users actually want."""
    rows = await db.pool().fetch("SELECT cuisines FROM profiles WHERE cuisines IS NOT NULL")
    counts: dict[str, int] = {}
    for row in rows:
        for cuisine in row["cuisines"] or []:
            counts[cuisine] = counts.get(cuisine, 0) + 1

    if not counts:
        return DEFAULT_CUISINES
    return sorted(counts, key=counts.get, reverse=True)


async def _generate_one_stub(cuisine: str) -> None:
    user_prompt = f"Invent a {cuisine} dinner recipe idea."
    raw = await cloudflare.generate_text(SYSTEM_PROMPT, user_prompt)
    idea = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])

    row = await db.pool().fetchrow(
        """
        INSERT INTO recipes (title, cuisine, main_protein, brief_description, image_prompt, status, source, verified)
        VALUES ($1, $2, $3, $4, $5, 'stub', 'cloudflare', false)
        RETURNING id
        """,
        idea["title"],
        cuisine,
        idea.get("main_protein"),
        idea["brief_description"],
        idea["image_prompt"],
    )

    vector = await cloudflare.embed_text(f"{idea['title']}. {idea['brief_description']}")
    await db.pool().execute(
        "UPDATE recipes SET embedding = $1::vector WHERE id = $2",
        cloudflare.vector_literal(vector),
        row["id"],
    )

    # Batch work, not a live request — queueing on failure is always fine here (step 35).
    image_url = await image_chain.generate_and_upload_image(str(row["id"]), idea["image_prompt"])
    if image_url:
        await db.pool().execute(
            "UPDATE recipes SET image_url = $1 WHERE id = $2", image_url, row["id"]
        )


async def run_pool_warmer() -> dict:
    cuisines = await _popular_cuisines()
    created = 0
    errors = []

    for _ in range(STUBS_PER_NIGHT):
        cuisine = random.choice(cuisines)
        try:
            await _generate_one_stub(cuisine)
            created += 1
        except Exception as exc:  # noqa: BLE001 — one bad stub shouldn't kill the batch
            errors.append(str(exc))

    return {"requested": STUBS_PER_NIGHT, "created": created, "errors": errors}


async def _main() -> None:
    await db.connect()
    try:
        result = await run_pool_warmer()
        print(result)
    finally:
        await db.disconnect()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
