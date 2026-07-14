import jwt
from fastapi import Depends, Header, HTTPException
from jwt import PyJWKClient

from app.core import db
from app.core.config import get_settings

# Supabase signs real user session tokens with an asymmetric ES256 key,
# published at this JWKS endpoint — not the legacy shared HS256 secret.
# PyJWKClient fetches and caches the public key, matching by the token's `kid`.
_jwks_clients: dict[str, PyJWKClient] = {}


def _get_jwks_client(supabase_url: str) -> PyJWKClient:
    if supabase_url not in _jwks_clients:
        _jwks_clients[supabase_url] = PyJWKClient(
            f"{supabase_url}/auth/v1/.well-known/jwks.json", cache_keys=True
        )
    return _jwks_clients[supabase_url]


async def get_current_user_id(authorization: str = Header(...)) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ")
    settings = get_settings()
    try:
        jwks_client = _get_jwks_client(settings.supabase_url)
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            audience="authenticated",
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
    return payload["sub"]


async def require_admin(user_id: str = Depends(get_current_user_id)) -> str:
    row = await db.pool().fetchrow("SELECT is_admin FROM profiles WHERE user_id = $1", user_id)
    if row is None or not row["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id


async def require_internal_secret(x_internal_secret: str = Header(default="")) -> None:
    """Shared-secret check for Cloud Scheduler-triggered cron endpoints."""
    settings = get_settings()
    if not settings.cron_secret or x_internal_secret != settings.cron_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


async def require_webhook_secret(x_internal_secret: str = Header(default="")) -> None:
    """Separate secret from require_internal_secret — deliberately distinct
    blast radius, since this one also lives in Supabase's vault (a different
    trust boundary than GCP's Cloud Run console/env vars)."""
    settings = get_settings()
    if not settings.webhook_secret or x_internal_secret != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
