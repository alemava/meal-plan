import httpx

from app.core.config import get_settings


async def upload_recipe_image(filename: str, image_bytes: bytes) -> str:
    settings = get_settings()
    url = f"{settings.supabase_url}/storage/v1/object/recipe-images/{filename}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            headers={
                "apikey": settings.supabase_service_key,
                "Authorization": f"Bearer {settings.supabase_service_key}",
                "Content-Type": "image/jpeg",
                "x-upsert": "true",
            },
            content=image_bytes,
        )
        resp.raise_for_status()
    return f"{settings.supabase_url}/storage/v1/object/public/recipe-images/{filename}"
