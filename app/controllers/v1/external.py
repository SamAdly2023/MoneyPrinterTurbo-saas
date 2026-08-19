"""
Public, API-key-authenticated endpoints for triggering Vidzy video generation
from another platform (Zapier, a custom app, a script, etc.). Users generate
their key and read usage docs from the "API" tab in the dashboard.

Auth is a bearer API key (Authorization: Bearer vdz_..., or an X-API-Key
header) - NOT the session cookie asgi.py's auth_gate normally requires. This
whole path prefix is exempted from that middleware (see asgi.py
_PUBLIC_PATHS's "/api/v1/external/" prefix check) and authenticates itself
here instead, the same way app/services/publish.py's OAuth callbacks are a
different trust boundary from the rest of the app.
"""

from typing import Optional

from fastapi import Request
from pydantic import BaseModel

from app.controllers.v1.base import new_router
from app.controllers.v1.saas import JobBody, _build_params, hash_api_key
from app.services import firestore_db, publish, saas
from app.utils import utils

router = new_router()

# Friendly, source-agnostic names - kept consistent with the dashboard's own
# style picker (see resource/public/index.html) so an external integrator
# never needs to know this maps to Pexels/Pollinations/Replicate internally.
_STYLE_MAP = {"stock": "pexels", "ai_visuals": "ai", "ai_presenter": "avatar"}


def _authenticate(request: Request) -> Optional[dict]:
    auth_header = request.headers.get("authorization", "")
    raw_key = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    if not raw_key:
        raw_key = (request.headers.get("x-api-key") or "").strip()
    if not raw_key:
        return None
    user = firestore_db.get_user_by_api_key_hash(hash_api_key(raw_key))
    if not user or user.get("is_disabled"):
        return None
    return user


def _video_urls(job: dict) -> list:
    base = publish.base_url()
    return [f"{base}{v}" for v in job.get("videos", [])]


def _video_response(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],
        "progress": job.get("progress", 0),
        "title": job.get("title", ""),
        "videos": _video_urls(job),
        "error": job.get("error", ""),
        "created_at": job.get("created_at", ""),
    }


class CreateVideoBody(BaseModel):
    subject: str
    script: Optional[str] = ""
    keywords: Optional[str] = ""
    title: Optional[str] = ""
    aspect_ratio: Optional[str] = None  # "9:16" | "16:9" | "1:1"
    style: Optional[str] = None  # "stock" | "ai_visuals" | "ai_presenter"


@router.post("/external/v1/videos", summary="Create a video generation job (API-key auth)")
def create_video(request: Request, body: CreateVideoBody):
    user = _authenticate(request)
    if not user:
        return utils.get_response(401, message="invalid or missing API key")
    if not (body.subject or "").strip():
        return utils.get_response(400, message="subject is required")

    video_source = None
    if body.style:
        style_key = body.style.strip().lower()
        if style_key not in _STYLE_MAP:
            return utils.get_response(
                400, message=f"invalid style '{body.style}' - use one of: {', '.join(_STYLE_MAP)}"
            )
        video_source = _STYLE_MAP[style_key]

    uid = user["uid"]
    global_settings = firestore_db.get_global_settings()
    profile = firestore_db.get_user_profile(uid)
    job_body = JobBody(
        title=body.title or body.subject,
        video_subject=body.subject,
        video_script=body.script or "",
        video_terms=body.keywords or "",
        video_aspect=body.aspect_ratio,
        video_source=video_source,
    )
    try:
        params = _build_params(global_settings, job_body, profile)
    except Exception as e:  # noqa: BLE001 - surface the validation reason to the caller
        return utils.get_response(400, message=f"invalid parameters: {e}")

    job = saas.create_job(uid, title=job_body.title, params=params)
    return utils.get_response(200, _video_response(job))


@router.get("/external/v1/videos/{job_id}", summary="Check a video job's status (API-key auth)")
def get_video(request: Request, job_id: str):
    user = _authenticate(request)
    if not user:
        return utils.get_response(401, message="invalid or missing API key")
    job = saas.store.get(user["uid"], job_id)
    if not job:
        return utils.get_response(404, message="job not found")
    return utils.get_response(200, _video_response(job))


@router.get("/external/v1/videos", summary="List your recent video jobs (API-key auth)")
def list_videos(request: Request, limit: int = 20):
    user = _authenticate(request)
    if not user:
        return utils.get_response(401, message="invalid or missing API key")
    jobs = saas.store.all(user["uid"])[: max(1, min(limit, 100))]
    return utils.get_response(200, {"videos": [_video_response(j) for j in jobs]})
