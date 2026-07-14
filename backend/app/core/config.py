from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"

    supabase_url_dev: str = ""
    supabase_service_key_dev: str = ""
    supabase_jwt_secret_dev: str = ""
    database_url_dev: str = ""

    supabase_url_prod: str = ""
    supabase_service_key_prod: str = ""
    supabase_jwt_secret_prod: str = ""
    database_url_prod: str = ""

    cf_account_id: str = ""
    cf_api_token: str = ""

    openrouter_api_key: str = ""
    groq_api_key: str = ""
    anthropic_api_key: str = ""

    resend_api_key: str = ""
    resend_sandbox_recipient: str = ""

    # DeepInfra — paid PAYG backstop when Cloudflare fails/limits (no free
    # tier on DeepInfra itself; ~$0.0005/image). Pollinations was removed from
    # the chain entirely (2026-07) after its Flux tier turned out to no
    # longer be free — verified against its live pricing page.
    deepinfra_api_key: str = ""

    sentry_dsn: str = ""
    cron_secret: str = ""
    webhook_secret: str = ""

    # Cloud Tasks — async steps-generation worker (2026-07-10). The queue is
    # itself the per-minute throttle (see cloud_tasks.py); backend_base_url
    # is where tasks call back into this same service.
    gcp_project_id: str = "mesa-501721"
    gcp_region: str = "europe-west2"
    steps_generation_queue: str = "steps-generation"
    backend_base_url: str = "https://mesa-backend-904247737916.europe-west2.run.app"

    # Async recipe-batch generation (2026-07-14) — separate queue from
    # steps-generation: very different payload/duration-per-attempt profile
    # (a full multi-slot batch vs. one recipe's steps). frontend_base_url
    # points notification emails back into the app — the ngrok tunnel is
    # deliberately what's used here, not the GitHub Pages prod URL, since
    # Generate is dev-only (BACKEND is null on the prod hostname, no prod
    # Cloud Run deploy exists yet) and the tunnel is the only origin that's
    # actually reachable from a real email link today.
    generate_recipes_queue: str = "generate-recipes-batch"
    frontend_base_url: str = "https://regroup-affluent-bunkhouse.ngrok-free.dev"

    # Paid escape hatches. image_paid_backstop defaults True: cheap
    # (~$0.0005/image) and, since Pollinations was removed, it's the only
    # real fallback left before the queue+placeholder. See cost_status.py's
    # monthly spend cap for the actual governance on this.
    text_paid_mode: bool = False
    image_paid_backstop: bool = True

    @property
    def supabase_url(self) -> str:
        return self.supabase_url_prod if self.app_env == "prod" else self.supabase_url_dev

    @property
    def supabase_service_key(self) -> str:
        return (
            self.supabase_service_key_prod
            if self.app_env == "prod"
            else self.supabase_service_key_dev
        )

    @property
    def database_url(self) -> str:
        return self.database_url_prod if self.app_env == "prod" else self.database_url_dev

    @property
    def supabase_jwt_secret(self) -> str:
        return (
            self.supabase_jwt_secret_prod
            if self.app_env == "prod"
            else self.supabase_jwt_secret_dev
        )

    @property
    def ai_provider(self) -> str:
        """Provider-agnostic switch: Anthropic if configured, else OpenRouter."""
        return "anthropic" if self.anthropic_api_key else "openrouter"

    @property
    def email_enabled(self) -> bool:
        """Sandbox mode: real Resend send, restricted to one verified recipient."""
        return bool(self.resend_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
