import httpx

from app.core.config import get_settings

RESEND_URL = "https://api.resend.com/emails"


async def send_email(subject: str, html: str) -> None:
    """Sandbox mode: sends from onboarding@resend.dev to the one verified
    recipient only (Alejandro + Ken for now, per the current MVP scope)."""
    settings = get_settings()
    if not settings.email_enabled:
        return

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": "onboarding@resend.dev",
                "to": [settings.resend_sandbox_recipient],
                "subject": subject,
                "html": html,
            },
        )
        resp.raise_for_status()
