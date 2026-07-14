from app.core import db

# Step 13's daily cap is disabled for now — see handover notes: the real
# limit value needs an actual product/cost decision, not a placeholder
# guessed during initial build. Still recording every attempt below so
# there's real usage data to base that decision on later.


async def check_and_record_generation(user_id: str, week_id: str) -> None:
    await db.pool().execute(
        "INSERT INTO generation_requests (user_id, week_id) VALUES ($1, $2)",
        user_id,
        week_id,
    )
