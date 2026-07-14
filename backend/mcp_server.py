"""Standalone MCP server (ADR 5) — exposes the same recipe-pool and history
functions used by the live tool-use loop, but to any MCP-compatible client,
not just mesa's own app. Runs in its own venv (venv-mcp/) deliberately
decoupled from the FastAPI app's dependencies — see requirements-mcp.txt.

Run with: venv-mcp/bin/python mcp_server.py
"""

from mcp.server.fastmcp import FastMCP

from app.core import db
from app.services import history as history_service
from app.services import pool_search
from app.services.profile import UserProfile

mcp = FastMCP("mesa-recipes")


NIL_USER_ID = (
    "00000000-0000-0000-0000-000000000000"  # "no user" sentinel — matches zero history rows
)


@mcp.tool()
async def search_recipe_pool(
    meal_type: str, cuisine: str = "", allergies: str = "", user_id: str = ""
) -> dict:
    """Search mesa's recipe pool for a recipe matching a meal type and cuisine.

    allergies: comma-separated allergen tags to exclude, if any.
    user_id: a real mesa user's UUID for personalized repeat-avoidance, or
        omit for an anonymous/generic search.
    """
    guest_profile = UserProfile(
        user_id=user_id or NIL_USER_ID,
        cuisines=[cuisine] if cuisine else [],
        allergies=[a.strip() for a in allergies.split(",") if a.strip()],
        dislikes=[],
        household_size=2,
        skill="intermediate",
    )
    result = await pool_search.search_recipe_pool(guest_profile, meal_type)
    if not result:
        return {"found": False}
    return {
        "found": True,
        "id": str(result["id"]),
        "title": result["title"],
        "brief_description": result["brief_description"],
        "cuisine": result["cuisine"],
        "status": result["status"],
    }


@mcp.tool()
async def get_user_history(user_id: str, days: int = 14) -> list[dict]:
    """Get a mesa user's recently cooked recipe titles and main proteins."""
    return await history_service.get_user_history(user_id, days=days)


async def _main() -> None:
    await db.connect()
    try:
        await mcp.run_stdio_async()
    finally:
        await db.disconnect()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
