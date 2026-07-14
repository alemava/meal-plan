from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sentry_sdk.integrations.fastapi import FastApiIntegration

from app.api import admin, generate_recipes, internal, select_recipe, translate_recipe, webhooks
from app.core import db
from app.core.config import get_settings

settings = get_settings()

if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        integrations=[FastApiIntegration()],
        traces_sample_rate=0.1,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    yield
    await db.disconnect()


app = FastAPI(title="mesa backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://alemava.github.io",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "https://regroup-affluent-bunkhouse.ngrok-free.dev",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "env": settings.app_env, "ai_provider": settings.ai_provider}


app.include_router(admin.router)
app.include_router(internal.router)
app.include_router(generate_recipes.router)
app.include_router(select_recipe.router)
app.include_router(translate_recipe.router)
app.include_router(webhooks.router)
