"""
Admin-only endpoints for the professional admin dashboard.

Every handler here re-checks request.state.user["is_admin"] itself (in
addition to asgi.py's page-level /admin gate) since these are real data-
and control-plane actions, not just page views.
"""

import os
import secrets
from typing import Optional

from fastapi import File, Path, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.controllers.v1.base import new_router
from app.services import firestore_db, saas, visitors
from app.services.auth import ADMIN_EMAILS
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
        "ai_visual_style": settings.get("ai_visual_style", ""),
        "auto_topic_template": settings.get("auto_topic_template", ""),
        "pexels_api_key": _first(settings.get("pexels_api_keys", [])),
        "pixabay_api_key": _first(settings.get("pixabay_api_keys", [])),
        "extra_token": settings.get("extra_token", ""),
        "llm_provider": settings.get("llm_provider", "groq"),
        "groq_api_key": settings.get("groq_api_key", ""),
        "groq_model_name": settings.get("groq_model_name", "openai/gpt-oss-120b"),
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
        "youtube_daily_cap": settings.get("youtube_daily_cap", 0),
        "tiktok_client_key": settings.get("tiktok_client_key", ""),
        "tiktok_client_secret": settings.get("tiktok_client_secret", ""),
        "facebook_app_id": settings.get("facebook_app_id", ""),
        "facebook_app_secret": settings.get("facebook_app_secret", ""),
        "publish_base_url": settings.get("publish_base_url", "http://localhost:8080"),
        "replicate_api_token": settings.get("replicate_api_token", ""),
        "avatar_image_path": settings.get("avatar_image_path", ""),
        # Transactional email (welcome email + admin new-signup alerts)
        "smtp_host": settings.get("smtp_host", ""),
        "smtp_port": settings.get("smtp_port", 587),
        "smtp_username": settings.get("smtp_username", ""),
        "smtp_password": settings.get("smtp_password", ""),
        "smtp_from_email": settings.get("smtp_from_email", ""),
        "smtp_from_name": settings.get("smtp_from_name", "Vidzy"),
        "admin_notify_email": settings.get("admin_notify_email", ""),
    }


class SettingsBody(BaseModel):
    video_source: Optional[str] = None
    ai_visual_style: Optional[str] = None
    auto_topic_template: Optional[str] = None
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
    youtube_daily_cap: Optional[int] = None
    tiktok_client_key: Optional[str] = None
    tiktok_client_secret: Optional[str] = None
    facebook_app_id: Optional[str] = None
    facebook_app_secret: Optional[str] = None
    publish_base_url: Optional[str] = None
    replicate_api_token: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from_email: Optional[str] = None
    smtp_from_name: Optional[str] = None
    admin_notify_email: Optional[str] = None


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
        is_owner = (u.get("email") or "").lower() in ADMIN_EMAILS
        u["is_owner"] = is_owner
        u["is_admin"] = is_owner or bool(u.get("is_admin"))
    users.sort(key=lambda u: u.get("created_at", ""), reverse=True)
    return utils.get_response(200, {"users": users})


@router.post("/admin/users/{uid}/promote", summary="Grant a user admin access")
def promote_user(request: Request, uid: str = Path(...)):
    if (denied := _require_admin(request)) is not None:
        return denied
    firestore_db.set_user_admin(uid, True)
    return utils.get_response(200, {"uid": uid, "is_admin": True})


@router.post("/admin/users/{uid}/demote", summary="Revoke a user's admin access")
def demote_user(request: Request, uid: str = Path(...)):
    if (denied := _require_admin(request)) is not None:
        return denied
    if uid == request.state.user["uid"]:
        return utils.get_response(400, message="cannot demote your own admin account")
    target = firestore_db.get_user(uid)
    if target and (target.get("email") or "").lower() in ADMIN_EMAILS:
        return utils.get_response(400, message="this account is a permanent owner and cannot be demoted")
    firestore_db.set_user_admin(uid, False)
    return utils.get_response(200, {"uid": uid, "is_admin": False})


# --------------------------------------------------------------------------- #
# Invites - signup is invitation-only, so an account starts here
# --------------------------------------------------------------------------- #
class InviteBody(BaseModel):
    email: str


@router.post("/admin/invites", summary="Create a personal sign-up link for one email")
def create_invite(request: Request, body: InviteBody):
    if (denied := _require_admin(request)) is not None:
        return denied
    email = (body.email or "").strip().lower()
    if "@" not in email or " " in email:
        return utils.get_response(400, message="enter a valid email address")

    # Bound to this address and single-use: the link is sent over WhatsApp,
    # so it must be worthless to anyone who forwards it on.
    token = secrets.token_urlsafe(24)
    firestore_db.create_invite(token, email, request.state.user["uid"])
    base = (firestore_db.get_global_settings().get("publish_base_url") or "").rstrip("/")
    return utils.get_response(200, {
        "token": token,
        "email": email,
        "link": f"{base}/login?invite={token}",
    })


@router.get("/admin/invites", summary="List invites and whether they were used")
def list_invites(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    base = (firestore_db.get_global_settings().get("publish_base_url") or "").rstrip("/")
    invites = firestore_db.list_invites()
    for inv in invites:
        inv["link"] = f"{base}/login?invite={inv.get('token','')}"
    return utils.get_response(200, {"invites": invites})


@router.delete("/admin/invites/{token}", summary="Revoke an unused invite")
def revoke_invite(request: Request, token: str = Path(...)):
    if (denied := _require_admin(request)) is not None:
        return denied
    firestore_db.delete_invite(token)
    return utils.get_response(200, {"token": token})


# --------------------------------------------------------------------------- #
# Per-user capability switches
# --------------------------------------------------------------------------- #
class FeaturesBody(BaseModel):
    can_render: Optional[bool] = None
    can_clip: Optional[bool] = None


@router.post("/admin/users/{uid}/features", summary="Turn rendering or clipping on/off for one user")
def set_user_features(request: Request, body: FeaturesBody, uid: str = Path(...)):
    if (denied := _require_admin(request)) is not None:
        return denied
    flags = {k: v for k, v in body.model_dump().items() if v is not None}
    if not flags:
        return utils.get_response(400, message="nothing to change")
    firestore_db.set_user_features(uid, **flags)
    return utils.get_response(200, {"uid": uid, **flags})


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


# --------------------------------------------------------------------------- #
# Visitor analytics (public marketing-page traffic - see app/services/visitors.py)
# --------------------------------------------------------------------------- #
@router.get("/admin/visitors/summary", summary="Visitor traffic summary")
def visitors_summary(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    sessions = firestore_db.list_visitor_sessions(limit=2000)
    return utils.get_response(200, visitors.summarize(sessions))


@router.get("/admin/visitors/sessions", summary="Recent visitor sessions")
def visitors_sessions(request: Request, limit: int = 300):
    if (denied := _require_admin(request)) is not None:
        return denied
    sessions = firestore_db.list_visitor_sessions(limit=min(max(limit, 1), 1000))
    return utils.get_response(200, {"sessions": sessions})


@router.get("/admin/visitors/export.csv", summary="Export visitor sessions as CSV")
def visitors_export(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    sessions = firestore_db.list_visitor_sessions(limit=5000)
    csv_text = visitors.export_csv(sessions)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=visitors.csv"},
    )


# --------------------------------------------------------------------------- #
# AI talking-avatar presenter photo (see app/services/avatar.py) - stored
# directly in the public /media/ output dir since Replicate's API needs to
# fetch it over HTTP, the same trust model already relied on for finished
# videos and Instagram's server-to-server fetch.
# --------------------------------------------------------------------------- #
ALLOWED_AVATAR_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_AVATAR_UPLOAD_BYTES = 8 * 1024 * 1024  # 8MB


@router.post("/admin/avatar-image", summary="Upload the platform-wide AI avatar presenter photo")
def upload_avatar_image(request: Request, file: UploadFile = File(...)):
    if (denied := _require_admin(request)) is not None:
        return denied
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_AVATAR_EXTS:
        return utils.get_response(400, message=f"unsupported image type: {ext or 'unknown'}")

    filename = f"_avatar-presenter{ext}"
    dest_path = os.path.join(saas.output_dir(), filename)
    size = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_AVATAR_UPLOAD_BYTES:
                    raise ValueError("image too large (max 8MB)")
                out.write(chunk)
    except Exception as e:
        if os.path.isfile(dest_path):
            os.remove(dest_path)
        return utils.get_response(400, message=f"upload failed: {e}")

    settings = firestore_db.get_global_settings()
    old_filename = settings.get("avatar_image_path") or ""
    if old_filename and old_filename != filename:
        old_path = os.path.join(saas.output_dir(), old_filename)
        if os.path.isfile(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass

    settings["avatar_image_path"] = filename
    firestore_db.save_global_settings(settings)
    return utils.get_response(200, _settings_response(settings))


@router.delete("/admin/avatar-image", summary="Remove the platform-wide AI avatar presenter photo")
def delete_avatar_image(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    settings = firestore_db.get_global_settings()
    filename = settings.get("avatar_image_path") or ""
    if filename:
        path = os.path.join(saas.output_dir(), filename)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    settings["avatar_image_path"] = ""
    firestore_db.save_global_settings(settings)
    return utils.get_response(200, _settings_response(settings))
