from dataclasses import dataclass

from app.core import db

# Neutral defaults for any user who hasn't been through onboarding (deferred to
# Phase 6). Only Alejandro + Ken have real migrated preferences right now —
# the system must never break on an empty/null profile.
DEFAULT_CUISINES = ["italian", "mexican", "asian", "mediterranean", "indian"]
DEFAULT_HOUSEHOLD_SIZE = 2
DEFAULT_SKILL = "intermediate"


@dataclass
class UserProfile:
    user_id: str
    cuisines: list[str]
    allergies: list[str]
    dislikes: list[str]
    household_size: int
    skill: str


async def load_profile(user_id: str) -> UserProfile:
    row = await db.pool().fetchrow(
        "SELECT cuisines, allergies, dislikes, household_size, skill "
        "FROM profiles WHERE user_id = $1",
        user_id,
    )

    return UserProfile(
        user_id=user_id,
        cuisines=(row["cuisines"] if row and row["cuisines"] else DEFAULT_CUISINES),
        allergies=(row["allergies"] if row and row["allergies"] else []),
        dislikes=(row["dislikes"] if row and row["dislikes"] else []),
        household_size=(
            row["household_size"] if row and row["household_size"] else DEFAULT_HOUSEHOLD_SIZE
        ),
        skill=(row["skill"] if row and row["skill"] else DEFAULT_SKILL),
    )
