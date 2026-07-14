from fastapi import APIRouter, Depends

from app.core.security import require_webhook_secret
from app.services import resend_client

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/meal-ready", dependencies=[Depends(require_webhook_secret)])
async def meal_ready(payload: dict):
    """Supabase DB webhook on meal_weeks INSERT (fires once per week, not on
    every later slot update — INSERT ... ON CONFLICT DO UPDATE only fires the
    INSERT trigger the first time a week_id is created for a user)."""
    record = payload.get("record") or {}
    week_id = record.get("id", "your week")

    await resend_client.send_email(
        "Your week is ready!",
        f"<p>Your meal plan for <strong>{week_id}</strong> is ready to view in mesa.</p>",
    )
    return {"status": "sent"}
