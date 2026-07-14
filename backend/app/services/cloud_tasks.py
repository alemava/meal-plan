import base64
import json

import httpx

from app.core.config import get_settings

# REST API directly via httpx (consistent with the rest of this codebase,
# which never pulls in the heavier google-cloud-* client libraries) rather
# than the google-cloud-tasks SDK.
_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/" "service-accounts/default/token"
)


async def _get_access_token() -> str:
    """Only works when actually running on Cloud Run/GCE — the metadata
    server isn't reachable from a local dev machine, so task creation can't
    be exercised outside the deployed service."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(_METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
        resp.raise_for_status()
        return resp.json()["access_token"]


async def enqueue_steps_generation(recipe_id: str, user_id: str) -> None:
    """Enqueues the async steps-generation task. The queue's own dispatch
    rate (0.25/sec, ~15/min — see the queue's gcloud config) is the
    per-minute throttle; retry/backoff on worker failure is the queue's
    own retry policy (max 50 attempts, 30s-600s backoff, 24h max duration)
    — no bespoke retry logic needed here, Cloud Tasks owns that."""
    settings = get_settings()
    access_token = await _get_access_token()

    worker_url = f"{settings.backend_base_url}/api/internal/generate-steps"
    body = json.dumps({"recipe_id": recipe_id, "user_id": user_id}).encode()

    task = {
        "task": {
            "httpRequest": {
                "url": worker_url,
                "httpMethod": "POST",
                "headers": {
                    "Content-Type": "application/json",
                    "X-Internal-Secret": settings.cron_secret,
                },
                "body": base64.b64encode(body).decode(),
            },
            "dispatchDeadline": "540s",  # headroom above ASYNC_WORKER_TIMEOUT_SECONDS
        }
    }

    queue_url = (
        f"https://cloudtasks.googleapis.com/v2/projects/{settings.gcp_project_id}"
        f"/locations/{settings.gcp_region}/queues/{settings.steps_generation_queue}/tasks"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            queue_url,
            headers={"Authorization": f"Bearer {access_token}"},
            json=task,
        )
        resp.raise_for_status()
