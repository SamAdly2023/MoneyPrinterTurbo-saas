"""
Admin-only endpoints for the professional admin dashboard.

Every handler here re-checks request.state.user["is_admin"] itself (in
addition to asgi.py's page-level /admin gate) since these are real data-
and control-plane actions, not just page views.
"""

from typing import Optional

from fastapi import Path, Request
from pydantic import BaseModel

from app.controllers.v1.base import new_router
from app.services import firestore_db, saas
from app.utils import utils

router = new_router()


# --------------------------------------------------------------------------- #
# Global settings (API keys / LLM / OAuth-app credentials / video defaults) -
# shared by every user's jobs, so only the admin can view/change them.
# --------------------------------------------------------------------------- #
def _first(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def _settings_response(settings: dict) -> dict:
    return {
        "video_source": settings.get("video_source", "pexels"),
        "pexels_api_key": _first(settings.get("pexels_api_keys", [])),
        "pixabay_api_key": _first(settings.get("pixabay_api_keys", [])),
        "extra_token": settings.get("extra_token", ""),
        "llm_provider": settings.get("llm_provider", "groq"),
        "groq_api_key": settings.get("groq_api_key", ""),
        "groq_model_name": settings.get("groq_model_name", "llama-3.3-70b-versatile"),
        "grok_api_key": settings.get("grok_api_key", ""),
        "grok_model_name": settings.get("grok_model_name", "grok-4.3"),
        "openai_api_key": settings.get("openai_api_key", ""),
        "openai_base_url": settings.get("openai_base_url", ""),
        "openai_model_name": settings.get("openai_model_name", "gpt-4o-mini"),
        # generation defaults (every job can still override these per-video)
        "voice_name": settings.get("voice_name", "en-US-AndrewNeural-Male"),
        "video_aspect": settings.get("video_aspect", "9:16"),
        "subtitle_enabled": settings.get("subtitle_enabled", True),
        "font_size": settings.get("font_size", 60),
        "subtitle_position": settings.get("subtitle_position", "bottom"),
        "paragraph_number": settings.get("paragraph_number", 1),
        "video_clip_duration": settings.get("video_clip_duration", 5),
        "bgm_type": settings.get("bgm_type", "random"),
        # publishing (OAuth app credentials - shared; connected accounts are per-user)
        "youtube_client_id": settings.get("youtube_client_id", ""),
        "youtube_client_secret": settings.get("youtube_client_secret", ""),
        "tiktok_client_key": settings.get("tiktok_client_key", ""),
        "tiktok_client_secret": settings.get("tiktok_client_secret", ""),
        "publish_base_url": settings.get("publish_base_url", "http://localhost:8080"),
    }


class SettingsBody(BaseModel):
    video_source: Optional[str] = None
    pexels_api_key: Optional[str] = None
    pixabay_api_key: Optional[str] = None
    extra_token: Optional[str] = None
    llm_provider: Optional[str] = None
    groq_api_key: Optional[str] = None
    groq_model_name: Optional[str] = None
    grok_api_key: Optional[str] = None
    grok_model_name: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    openai_model_name: Optional[str] = None
    voice_name: Optional[str] = None
    video_aspect: Optional[str] = None
    subtitle_enabled: Optional[bool] = None
    font_size: Optional[int] = None
    subtitle_position: Optional[str] = None
    paragraph_number: Optional[int] = None
    video_clip_duration: Optional[int] = None
    bgm_type: Optional[str] = None
    youtube_client_id: Optional[str] = None
    youtube_client_secret: Optional[str] = None
    tiktok_client_key: Optional[str] = None
    tiktok_client_secret: Optional[str] = None
    publish_base_url: Optional[str] = None


@router.get("/admin/settings", summary="Get the shared API keys / integration settings")
def get_settings(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    settings = firestore_db.get_global_settings()
    return utils.get_response(200, _settings_response(settings))


@router.post("/admin/settings", summary="Save the shared API keys / integration settings")
def save_settings(request: Request, body: SettingsBody):
    if (denied := _require_admin(request)) is not None:
        return denied
    settings = firestore_db.get_global_settings()
    data = body.model_dump(exclude_none=True)

    if "pexels_api_key" in data:
        key = data.pop("pexels_api_key").strip()
        settings["pexels_api_keys"] = [key] if key else []
    if "pixabay_api_key" in data:
        key = data.pop("pixabay_api_key").strip()
        settings["pixabay_api_keys"] = [key] if key else []

    settings.update(data)
    firestore_db.save_global_settings(settings)
    return utils.get_response(200, _settings_response(settings))


def _require_admin(request: Request):
    user = request.state.user
    if not user.get("is_admin"):
        return utils.get_response(403, message="admin only")
    return None


@router.get("/admin/overview", summary="Admin dashboard overview stats")
def overview(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied

    users = firestore_db.list_users()
    jobs = saas.store.all_admin()
    counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    for j in jobs:
        counts[j.get("status", "")] = counts.get(j.get("status", ""), 0) + 1
    return utils.get_response(
        200,
        {
            "total_users": len(users),
            "disabled_users": sum(1 for u in users if u.get("is_disabled")),
            "auto_mode_users": sum(1 for u in users if u.get("profile", {}).get("auto_mode")),
            "total_videos": counts["done"],
            "job_counts": counts,
        },
    )


@router.get("/admin/users", summary="List every signed-up user")
def list_users(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied

    users = firestore_db.list_users()
    jobs = saas.store.all_admin()
    job_counts_by_uid = {}
    for j in jobs:
        job_counts_by_uid[j["uid"]] = job_counts_by_uid.get(j["uid"], 0) + 1
    for u in users:
        u["job_count"] = job_counts_by_uid.get(u["uid"], 0)
    users.sort(key=lambda u: u.get("created_at", ""), reverse=True)
    return utils.get_response(200, {"users": users})


@router.post("/admin/users/{uid}/disable", summary="Disable a user's account")
def disable_user(request: Request, uid: str = Path(...)):
    if (denied := _require_admin(request)) is not None:
        return denied
    if uid == request.state.user["uid"]:
        return utils.get_response(400, message="cannot disable your own admin account")
    firestore_db.set_user_disabled(uid, True)
    return utils.get_response(200, {"uid": uid, "is_disabled": True})


@router.post("/admin/users/{uid}/enable", summary="Re-enable a user's account")
def enable_user(request: Request, uid: str = Path(...)):
    if (denied := _require_admin(request)) is not None:
        return denied
    firestore_db.set_user_disabled(uid, False)
    return utils.get_response(200, {"uid": uid, "is_disabled": False})


@router.get("/admin/jobs", summary="Every job across every user")
def list_all_jobs(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    return utils.get_response(200, {"jobs": saas.store.all_admin()})


@router.get("/admin/engine", summary="Engine status")
def engine_status(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    return utils.get_response(200, saas.engine.status())


@router.post("/admin/engine/pause", summary="Pause the shared render queue for everyone")
def engine_pause(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    saas.engine.pause()
    return utils.get_response(200, saas.engine.status())


@router.post("/admin/engine/resume", summary="Resume the shared render queue")
def engine_resume(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    saas.engine.resume()
    return utils.get_response(200, saas.engine.status())


@router.post("/admin/engine/auto-kill/start", summary="Globally disable everyone's auto-mode")
def auto_kill_start(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    saas.engine.auto_kill_start()
    return utils.get_response(200, saas.engine.status())


@router.post("/admin/engine/auto-kill/stop", summary="Re-allow auto-mode generation")
def auto_kill_stop(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    saas.engine.auto_kill_stop()
    return utils.get_response(200, saas.engine.status())
