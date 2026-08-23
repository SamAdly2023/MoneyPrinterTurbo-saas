"""
Dashboard API for the local SaaS layer (multi-tenant).

Endpoints (all under /api/v1), every one of them scoped to the calling
user's own Firestore data (request.state.user["uid"], set by asgi.py's
auth_gate middleware after verifying the session cookie):

    GET    /saas/profile               read this user's business profile
    POST   /saas/profile               persist this user's business profile
    GET    /saas/jobs                  list this user's queued/finished jobs
    POST   /saas/jobs                  save a script and queue it
    DELETE /saas/jobs/{job_id}         remove one of this user's jobs
    POST   /saas/jobs/{job_id}/retry   re-queue a failed/finished job
    GET    /saas/engine                shared engine status + this user's auto-mode flag
    POST   /saas/engine/auto/start     enable this user's auto-mode
    POST   /saas/engine/auto/stop      disable this user's auto-mode
    POST   /saas/generate-script       AI-generate a script, branded for this user's business

API keys / LLM / OAuth-app credentials are admin-only now (app_config/global
in Firestore, GET/POST /api/v1/admin/settings in admin.py) - every user's
jobs share them. Engine pause/resume and the global auto-mode kill switch
are admin-only too, for the same reason: letting any signed-up user touch
either would be a straightforward abuse vector.
"""

import hashlib
import os
import secrets
import shutil
from typing import Optional

from fastapi import File, Path, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from loguru import logger
from pydantic import BaseModel

from app.controllers.v1.base import new_router
from app.models.schema import MaterialInfo, VideoParams
from app.services import auth, clips, firestore_db, llm, publish, saas
from app.utils import utils

router = new_router()

# In-memory only (single-process engine, same assumption as saas.Engine):
# upload_id -> {uid, path, duration, filename, transcript_segments, segments}.
# Cleared once the user queues clip jobs from it; the underlying file is only
# deleted later, once no clip job still references it (see
# saas.Engine._cleanup_source_if_unused).
_CLIP_UPLOADS: dict = {}


def _blocked(request: Request, flag: str, label: str):
    """403 when an admin has switched this capability off for this account.

    The flag is re-read from the database on every request (see
    auth.get_current_user), so revoking it takes effect immediately - an
    open dashboard cannot keep queueing work on a stale session.
    """
    if not request.state.user.get(flag, True):
        return utils.get_response(
            403, message=f"{label} has been disabled for this account. Contact support."
        )
    return None


def _uid(request: Request) -> str:
    return request.state.user["uid"]


# --------------------------------------------------------------------------- #
# Business profile (per user - not API keys, see admin.py for those)
# --------------------------------------------------------------------------- #
def _profile_response(profile: dict) -> dict:
    return {
        "business_name": profile.get("business_name", ""),
        "business_address": profile.get("business_address", ""),
        "business_website": profile.get("business_website", ""),
        "business_email": profile.get("business_email", ""),
        "business_phone": profile.get("business_phone", ""),
        "business_bio": profile.get("business_bio", ""),
        "auto_publish": profile.get("auto_publish", False),
        "auto_publish_platforms": profile.get("auto_publish_platforms", []),
        "youtube_privacy": profile.get("youtube_privacy", "public"),
        "auto_mode": profile.get("auto_mode", False),
        # This account's own video preferences - override the admin's platform
        # defaults for this user's Auto Mode videos when set.
        "video_aspect": profile.get("video_aspect", ""),
        "subtitle_position": profile.get("subtitle_position", ""),
        "subtitle_pref": profile.get("subtitle_pref", "default"),
        "use_logo": profile.get("use_logo", False),
        "has_logo": bool(profile.get("logo_path")),
        "has_avatar_photo": bool(profile.get("avatar_image_path")),
        "avatar_image_path": profile.get("avatar_image_path", ""),
    }


class ProfileBody(BaseModel):
    business_name: Optional[str] = None
    business_address: Optional[str] = None
    business_website: Optional[str] = None
    business_email: Optional[str] = None
    business_phone: Optional[str] = None
    business_bio: Optional[str] = None
    auto_publish: Optional[bool] = None
    auto_publish_platforms: Optional[list] = None
    video_aspect: Optional[str] = None
    subtitle_position: Optional[str] = None
    subtitle_pref: Optional[str] = None
    youtube_privacy: Optional[str] = None
    use_logo: Optional[bool] = None


@router.get("/saas/profile", summary="Get this user's business profile")
def get_profile(request: Request):
    profile = firestore_db.get_user_profile(_uid(request))
    return utils.get_response(200, _profile_response(profile))


@router.post("/saas/profile", summary="Save this user's business profile")
def save_profile(request: Request, body: ProfileBody):
    uid = _uid(request)
    profile = firestore_db.get_user_profile(uid)
    data = body.model_dump(exclude_none=True)
    profile.update(data)
    firestore_db.save_user_profile(uid, profile)
    logger.info(f"profile saved for {uid}")
    return utils.get_response(200, _profile_response(profile))


ALLOWED_LOGO_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_LOGO_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB - a small watermark image, not a photo library


@router.post("/saas/profile/logo", summary="Upload this business's logo watermark")
def upload_logo(request: Request, file: UploadFile = File(...)):
    uid = _uid(request)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_LOGO_EXTS:
        return utils.get_response(400, message=f"unsupported image type: {ext or 'unknown'}")

    user_dir = os.path.join(saas.logos_dir(), uid)
    os.makedirs(user_dir, exist_ok=True)
    dest_path = os.path.join(user_dir, f"logo{ext}")

    size = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_LOGO_UPLOAD_BYTES:
                    raise ValueError("image too large (max 5MB)")
                out.write(chunk)
    except Exception as e:
        if os.path.isfile(dest_path):
            os.remove(dest_path)
        return utils.get_response(400, message=f"upload failed: {e}")

    # Only one logo per business - if a previous upload used a different
    # extension, drop it so logos_dir() doesn't accumulate stale files.
    profile = firestore_db.get_user_profile(uid)
    old_rel = profile.get("logo_path") or ""
    if old_rel and old_rel != f"{uid}/logo{ext}":
        old_abs = os.path.join(saas.logos_dir(), old_rel)
        if os.path.isfile(old_abs):
            try:
                os.remove(old_abs)
            except OSError:
                pass

    profile["logo_path"] = f"{uid}/logo{ext}"
    # Uploading a logo is a clear signal the business wants it used - default
    # to on immediately instead of requiring a separate toggle the user may
    # not notice, which otherwise silently ships every video with no logo.
    profile["use_logo"] = True
    firestore_db.save_user_profile(uid, profile)
    logger.info(f"logo uploaded for {uid}")
    return utils.get_response(200, _profile_response(profile))


@router.get("/saas/profile/logo", summary="Fetch this business's uploaded logo image")
def get_logo(request: Request):
    uid = _uid(request)
    profile = firestore_db.get_user_profile(uid)
    rel = profile.get("logo_path") or ""
    abs_path = os.path.join(saas.logos_dir(), rel) if rel else ""
    if not rel or not os.path.isfile(abs_path):
        return utils.get_response(404, message="no logo uploaded")
    return FileResponse(abs_path)


@router.delete("/saas/profile/logo", summary="Remove this business's logo watermark")
def delete_logo(request: Request):
    uid = _uid(request)
    profile = firestore_db.get_user_profile(uid)
    rel = profile.get("logo_path") or ""
    if rel:
        abs_path = os.path.join(saas.logos_dir(), rel)
        if os.path.isfile(abs_path):
            try:
                os.remove(abs_path)
            except OSError:
                pass
    profile["logo_path"] = ""
    profile["use_logo"] = False
    firestore_db.save_user_profile(uid, profile)
    return utils.get_response(200, _profile_response(profile))


ALLOWED_AVATAR_PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_AVATAR_PHOTO_BYTES = 8 * 1024 * 1024  # 8MB


@router.post("/saas/profile/avatar-photo", summary="Upload this business's AI talking-avatar presenter photo")
def upload_avatar_photo(request: Request, file: UploadFile = File(...)):
    uid = _uid(request)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_AVATAR_PHOTO_EXTS:
        return utils.get_response(400, message=f"unsupported image type: {ext or 'unknown'}")

    # Unlike the logo (composited locally), Replicate's servers must be able
    # to fetch this file over HTTP - it lives in the public output dir, the
    # same trust model already relied on for finished videos (see asgi.py).
    filename = f"_avatar-user-{uid}{ext}"
    dest_path = os.path.join(saas.output_dir(), filename)
    size = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_AVATAR_PHOTO_BYTES:
                    raise ValueError("image too large (max 8MB)")
                out.write(chunk)
    except Exception as e:
        if os.path.isfile(dest_path):
            os.remove(dest_path)
        return utils.get_response(400, message=f"upload failed: {e}")

    profile = firestore_db.get_user_profile(uid)
    old_filename = profile.get("avatar_image_path") or ""
    if old_filename and old_filename != filename:
        old_path = os.path.join(saas.output_dir(), old_filename)
        if os.path.isfile(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass

    profile["avatar_image_path"] = filename
    firestore_db.save_user_profile(uid, profile)
    logger.info(f"avatar photo uploaded for {uid}")
    return utils.get_response(200, _profile_response(profile))


@router.delete("/saas/profile/avatar-photo", summary="Remove this business's AI talking-avatar presenter photo")
def delete_avatar_photo(request: Request):
    uid = _uid(request)
    profile = firestore_db.get_user_profile(uid)
    filename = profile.get("avatar_image_path") or ""
    if filename:
        path = os.path.join(saas.output_dir(), filename)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    profile["avatar_image_path"] = ""
    firestore_db.save_user_profile(uid, profile)
    return utils.get_response(200, _profile_response(profile))


# --------------------------------------------------------------------------- #
# Jobs (the script queue)
# --------------------------------------------------------------------------- #
class JobBody(BaseModel):
    title: Optional[str] = ""
    video_subject: str
    video_script: Optional[str] = ""
    video_terms: Optional[str] = ""
    video_source: Optional[str] = None
    video_aspect: Optional[str] = None
    voice_name: Optional[str] = None
    subtitle_enabled: Optional[bool] = None
    video_clip_duration: Optional[int] = None
    paragraph_number: Optional[int] = None
    video_count: Optional[int] = 1
    bgm_type: Optional[str] = None
    font_size: Optional[int] = None
    subtitle_position: Optional[str] = None
    use_logo: Optional[bool] = None
    video_concat_mode: Optional[str] = None
    video_transition_mode: Optional[str] = None
    voice_rate: Optional[float] = None
    # rel_paths returned by POST /saas/materials/upload - your own photos/videos
    # used as this job's visuals instead of stock/AI/avatar (see "My Media").
    video_materials: Optional[list] = None


def _build_params(global_settings: dict, body: JobBody, profile: dict = None) -> dict:
    profile = profile or {}
    script_prompt = saas.SCRIPT_LENGTH_PROMPT
    if not (body.video_script or "").strip():
        # Only relevant when the pipeline will auto-write the script itself.
        business_context = saas._business_context_prompt(profile) if profile else ""
        if business_context:
            script_prompt = script_prompt + " " + business_context

    # Fallback chain: explicit form value -> this user's own saved preference -> platform default.
    profile_aspect = (profile.get("video_aspect") or "").strip()
    profile_subpos = (profile.get("subtitle_position") or "").strip()
    profile_subtitle_pref = profile.get("subtitle_pref", "default")
    if body.subtitle_enabled is not None:
        subtitle_enabled = body.subtitle_enabled
    elif profile_subtitle_pref == "on":
        subtitle_enabled = True
    elif profile_subtitle_pref == "off":
        subtitle_enabled = False
    else:
        subtitle_enabled = global_settings.get("subtitle_enabled", True)

    effective_use_logo = body.use_logo if body.use_logo is not None else profile.get("use_logo", False)
    logo_path = saas.resolve_logo_path(profile, effective_use_logo)
    contact_website, contact_phone = saas.contact_card_fields(profile)
    avatar_photo_path = saas.resolve_avatar_photo_path(profile)
    video_materials = (
        [MaterialInfo(provider="local", url=rel_path) for rel_path in body.video_materials]
        if body.video_materials else None
    )

    raw = {
        "video_subject": body.video_subject.strip(),
        "video_script": (body.video_script or "").strip(),
        "video_terms": (body.video_terms or "").strip() or None,
        # Uploading your own media implies "use it" even if the style picker
        # still says platform default - no need to also flip the dropdown.
        "video_source": body.video_source or ("local" if video_materials else "") or global_settings.get("video_source", "pexels"),
        "video_materials": video_materials,
        "video_aspect": body.video_aspect or profile_aspect or global_settings.get("video_aspect", "9:16"),
        "voice_name": body.voice_name or global_settings.get("voice_name", "en-US-AndrewNeural-Male"),
        "subtitle_enabled": subtitle_enabled,
        "video_clip_duration": body.video_clip_duration or global_settings.get("video_clip_duration", 5),
        "paragraph_number": body.paragraph_number or global_settings.get("paragraph_number", saas.DEFAULT_PARAGRAPHS),
        "video_count": body.video_count or 1,
        "bgm_type": body.bgm_type or global_settings.get("bgm_type", "random"),
        "font_size": body.font_size or global_settings.get("font_size", 60),
        "subtitle_position": body.subtitle_position or profile_subpos or global_settings.get("subtitle_position", "bottom"),
        "font_name": global_settings.get("font_name", "MicrosoftYaHeiBold.ttc"),
        "text_fore_color": global_settings.get("text_fore_color", "#FFFFFF"),
        "video_script_prompt": script_prompt,
        "video_concat_mode": body.video_concat_mode or "random",
        "video_transition_mode": body.video_transition_mode or None,
        "voice_rate": body.voice_rate or 1.0,
        "logo_path": logo_path,
        "business_context": saas._business_niche_label(profile),
        "contact_website": contact_website,
        "contact_phone": contact_phone,
        "avatar_photo_path": avatar_photo_path,
    }
    # Validate against the real schema; raises on bad input.
    return VideoParams(**raw).model_dump()


@router.get("/saas/jobs", summary="List this user's jobs")
def list_jobs(request: Request):
    uid = _uid(request)
    jobs = saas.store.all(uid)
    counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    for j in jobs:
        counts[j["status"]] = counts.get(j["status"], 0) + 1
    status = saas.engine.status()
    status["auto_mode"] = firestore_db.get_user_profile(uid).get("auto_mode", False)
    return utils.get_response(200, {"jobs": jobs, "counts": counts, "engine": status})


@router.post("/saas/jobs", summary="Save a script and add it to the queue")
def create_job(request: Request, body: JobBody):
    if (denied := _blocked(request, "can_render", "Video rendering")) is not None:
        return denied
    if not body.video_subject.strip():
        return utils.get_response(400, message="video_subject is required")
    uid = _uid(request)
    global_settings = firestore_db.get_global_settings()
    profile = firestore_db.get_user_profile(uid)
    try:
        params = _build_params(global_settings, body, profile)
    except Exception as e:
        return utils.get_response(400, message=f"invalid parameters: {e}")
    job = saas.create_job(uid, title=body.title or body.video_subject, params=params)
    return utils.get_response(200, job)


@router.delete("/saas/jobs/{job_id}", summary="Delete a job")
def delete_job(request: Request, job_id: str = Path(...)):
    uid = _uid(request)
    job = saas.store.get(uid, job_id)
    if not job:
        return utils.get_response(404, message="job not found")
    # remove copied output files (videos + publishing kit)
    urls = list(job.get("videos", []))
    if job.get("meta_file"):
        urls.append(job["meta_file"])
    for url in urls:
        name = os.path.basename(url)
        path = os.path.join(saas.output_dir(), name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    saas.store.delete(uid, job_id)
    return utils.get_response(200, {"deleted": job_id})


@router.post("/saas/jobs/{job_id}/retry", summary="Re-queue a job")
def retry_job(request: Request, job_id: str = Path(...)):
    if (denied := _blocked(request, "can_render", "Video rendering")) is not None:
        return denied
    uid = _uid(request)
    job = saas.store.get(uid, job_id)
    if not job:
        return utils.get_response(404, message="job not found")
    saas.store.update(uid, job_id, status=saas.STATUS_PENDING, progress=0, error="", videos=[])
    saas.engine.wake()
    return utils.get_response(200, saas.store.get(uid, job_id))


# --------------------------------------------------------------------------- #
# Engine (read-only for regular users; control lives under /admin)
# --------------------------------------------------------------------------- #
@router.get("/saas/engine", summary="Shared engine status + this user's auto-mode flag")
def engine_status(request: Request):
    uid = _uid(request)
    status = saas.engine.status()
    status["auto_mode"] = firestore_db.get_user_profile(uid).get("auto_mode", False)
    return utils.get_response(200, status)


@router.post("/saas/engine/auto/start", summary="Enable this user's auto-mode")
def engine_auto_start(request: Request):
    uid = _uid(request)
    if not saas.auto_mode_available():
        # The UI hides the button here, but the endpoint is public - don't
        # rely on the client to enforce where generation is allowed.
        return utils.get_response(
            400, {}, "Auto Mode runs on the local app, not on the hosted site."
        )
    profile = firestore_db.get_user_profile(uid)
    profile["auto_mode"] = True
    firestore_db.save_user_profile(uid, profile)
    saas.engine.wake()
    status = saas.engine.status()
    status["auto_mode"] = True
    return utils.get_response(200, status)


@router.post("/saas/engine/auto/stop", summary="Disable this user's auto-mode")
def engine_auto_stop(request: Request):
    uid = _uid(request)
    profile = firestore_db.get_user_profile(uid)
    profile["auto_mode"] = False
    firestore_db.save_user_profile(uid, profile)
    status = saas.engine.status()
    status["auto_mode"] = False
    return utils.get_response(200, status)


class GenerateOneBody(BaseModel):
    video_source: Optional[str] = None
    video_aspect: Optional[str] = None
    video_materials: Optional[list] = None


@router.post("/saas/generate-one", summary="Generate a single AI video right now, without turning on Auto Mode")
def generate_one(request: Request, body: GenerateOneBody = None):
    if (denied := _blocked(request, "can_render", "Video rendering")) is not None:
        return denied
    uid = _uid(request)
    profile = firestore_db.get_user_profile(uid)
    video_source = (body.video_source if body else "") or ""
    video_aspect = (body.video_aspect if body else "") or ""
    video_materials = (body.video_materials if body else None) or None
    if video_source == "local" and not video_materials:
        return utils.get_response(400, message="upload at least one photo or video for 'My Media' style")
    try:
        with saas._user_config_scope(uid):
            job = saas.generate_viral_job(
                uid, profile, video_source=video_source, video_aspect=video_aspect, video_materials=video_materials,
            )
    except Exception as e:  # noqa: BLE001 - surface the LLM/idea-generation error to the UI
        logger.error(f"generate-one failed for {uid}: {e}")
        return utils.get_response(400, message=f"could not generate a video idea: {e}")
    return utils.get_response(200, job)


# --------------------------------------------------------------------------- #
# API keys (external platform access - see app/controllers/v1/external.py,
# which authenticates incoming requests with these same hashes)
# --------------------------------------------------------------------------- #
API_KEY_PREFIX = "vdz_"


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _api_key_response(uid: str) -> dict:
    user = firestore_db.get_user(uid) or {}
    return {
        "has_key": bool(user.get("api_key_hash")),
        "key_prefix": user.get("api_key_prefix", ""),
        "created_at": user.get("api_key_created_at", ""),
    }


@router.get("/saas/api-key", summary="This user's API key status (never returns the raw key)")
def get_api_key(request: Request):
    return utils.get_response(200, _api_key_response(_uid(request)))


@router.post("/saas/api-key/generate", summary="Generate (or replace) this user's API key")
def generate_api_key(request: Request):
    uid = _uid(request)
    raw_key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    firestore_db.set_user_api_key(uid, hash_api_key(raw_key), raw_key[: len(API_KEY_PREFIX) + 8])
    logger.info(f"API key generated for {uid}")
    resp = _api_key_response(uid)
    # Only time the raw key is ever returned - shown once, like a normal API-key UX.
    resp["api_key"] = raw_key
    return utils.get_response(200, resp)


@router.delete("/saas/api-key", summary="Revoke this user's API key")
def revoke_api_key(request: Request):
    uid = _uid(request)
    firestore_db.clear_user_api_key(uid)
    logger.info(f"API key revoked for {uid}")
    return utils.get_response(200, _api_key_response(uid))


# --------------------------------------------------------------------------- #
# Social publishing (YouTube / TikTok)
# --------------------------------------------------------------------------- #
def _popup_close_html(message: str, ok: bool = True) -> HTMLResponse:
    color = "#22c55e" if ok else "#ef4444"
    icon = "✅" if ok else "⚠️"
    html = f"""<!doctype html><html><body style="background:#0b0f19;color:#e7ecf5;
    font-family:system-ui,Segoe UI,sans-serif;display:grid;place-items:center;height:100vh;margin:0">
    <div style="text-align:center">
      <div style="font-size:44px">{icon}</div>
      <p style="color:{color};font-weight:700;font-size:18px">{message}</p>
      <p style="color:#8b98b5">You can close this window.</p>
    </div>
    <script>
      try {{ if (window.opener) window.opener.postMessage("mpt-social-updated", "*"); }} catch (e) {{}}
      setTimeout(function(){{ window.close(); }}, 1200);
    </script></body></html>"""
    return HTMLResponse(content=html)


@router.get("/saas/social/status", summary="Connection status for YouTube/TikTok")
def social_status(request: Request):
    uid = _uid(request)
    data = publish.status(uid)
    profile = firestore_db.get_user_profile(uid)
    data["settings"] = {
        "auto_publish": profile.get("auto_publish", False),
        "auto_publish_platforms": profile.get("auto_publish_platforms", []),
        "youtube_privacy": profile.get("youtube_privacy", "public"),
        "publish_base_url": firestore_db.get_global_settings().get("publish_base_url", "http://localhost:8080"),
    }
    return utils.get_response(200, data)


@router.get("/saas/{platform}/connect", summary="Get the OAuth URL for a platform")
def social_connect(request: Request, platform: str = Path(...)):
    try:
        if platform == "youtube":
            url = publish.youtube_auth_url()
        elif platform == "tiktok":
            url = publish.tiktok_auth_url()
        elif platform == "facebook":
            url = publish.facebook_auth_url()
        else:
            return utils.get_response(400, message="unknown platform")
    except ValueError as e:
        return utils.get_response(400, message=str(e))
    return utils.get_response(200, {"auth_url": url})


@router.get("/saas/youtube/callback", summary="YouTube OAuth callback")
def youtube_callback(request: Request, code: str = "", error: str = ""):
    # Same-origin popup still carries the session cookie, so we know who's connecting.
    user = auth.get_current_user(request)
    if not user:
        return _popup_close_html("Your session expired - please log in and try again.", ok=False)
    if error or not code:
        return _popup_close_html(f"YouTube connection failed: {error or 'no code'}", ok=False)
    try:
        info = publish.youtube_exchange_code(user["uid"], code)
        return _popup_close_html(f"YouTube connected: {info.get('channel') or 'channel'}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"youtube callback failed: {e}")
        return _popup_close_html(f"YouTube connection failed: {e}", ok=False)


@router.get("/saas/tiktok/callback", summary="TikTok OAuth callback")
def tiktok_callback(request: Request, code: str = "", error: str = ""):
    user = auth.get_current_user(request)
    if not user:
        return _popup_close_html("Your session expired - please log in and try again.", ok=False)
    if error or not code:
        return _popup_close_html(f"TikTok connection failed: {error or 'no code'}", ok=False)
    try:
        publish.tiktok_exchange_code(user["uid"], code)
        return _popup_close_html("TikTok connected")
    except Exception as e:  # noqa: BLE001
        logger.error(f"tiktok callback failed: {e}")
        return _popup_close_html(f"TikTok connection failed: {e}", ok=False)


@router.get("/saas/facebook/callback", summary="Facebook/Instagram OAuth callback")
def facebook_callback(request: Request, code: str = "", error: str = ""):
    user = auth.get_current_user(request)
    if not user:
        return _popup_close_html("Your session expired - please log in and try again.", ok=False)
    if error or not code:
        return _popup_close_html(f"Facebook connection failed: {error or 'no code'}", ok=False)
    try:
        info = publish.facebook_exchange_code(user["uid"], code)
        label = info.get("page_name") or "Page"
        if info.get("ig_username"):
            label += f" + Instagram @{info['ig_username']}"
        return _popup_close_html(f"Facebook connected: {label}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"facebook callback failed: {e}")
        return _popup_close_html(f"Facebook connection failed: {e}", ok=False)


@router.post("/saas/{platform}/disconnect", summary="Disconnect a platform")
def social_disconnect(request: Request, platform: str = Path(...)):
    if platform not in ("youtube", "tiktok", "facebook"):
        return utils.get_response(400, message="unknown platform")
    uid = _uid(request)
    publish.disconnect(uid, platform)
    return utils.get_response(200, publish.status(uid))


class PublishBody(BaseModel):
    platforms: Optional[list] = None


@router.post("/saas/jobs/{job_id}/publish", summary="Publish a finished job's video")
def publish_job(request: Request, job_id: str = Path(...), body: PublishBody = None):
    uid = _uid(request)
    job = saas.store.get(uid, job_id)
    if not job:
        return utils.get_response(404, message="job not found")
    if job["status"] != saas.STATUS_DONE or not job.get("videos"):
        return utils.get_response(400, message="job has no finished video to publish")

    platforms = (body.platforms if body else None) or ["youtube"]
    video_name = os.path.basename(job["videos"][0])
    video_path = os.path.join(saas.output_dir(), video_name)
    results = publish.publish_video(uid, video_path, job.get("meta", {}), platforms)

    merged = dict(job.get("publish", {}))
    merged.update(results)
    saas.store.update(uid, job_id, publish=merged)
    return utils.get_response(200, results)


# --------------------------------------------------------------------------- #
# "My Media": upload your own photos/videos to use as a job's visuals
# instead of stock footage/AI visuals/an AI presenter (video_source=="local",
# already fully supported by video.py's preprocess_video() - this is just
# the missing upload endpoint for it).
# --------------------------------------------------------------------------- #
ALLOWED_MATERIAL_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".mkv", ".webm", ".m4v"}
MAX_MATERIAL_UPLOAD_BYTES = 300 * 1024 * 1024  # 300MB per file
MAX_MATERIAL_FILES_PER_UPLOAD = 20
_MATERIAL_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


@router.post("/saas/materials/upload", summary="Upload photos/videos to use as this job's own visuals")
def upload_materials(request: Request, files: list[UploadFile] = File(...)):
    uid = _uid(request)
    if len(files) > MAX_MATERIAL_FILES_PER_UPLOAD:
        return utils.get_response(400, message=f"too many files - max {MAX_MATERIAL_FILES_PER_UPLOAD} per upload")

    user_dir = os.path.join(saas.materials_dir(), uid)
    os.makedirs(user_dir, exist_ok=True)
    uploaded = []
    for file in files:
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in ALLOWED_MATERIAL_EXTS:
            return utils.get_response(400, message=f"unsupported file type: {ext or 'unknown'} ({file.filename})")

        material_id = utils.get_uuid()
        dest_path = os.path.join(user_dir, f"{material_id}{ext}")
        size = 0
        try:
            with open(dest_path, "wb") as out:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_MATERIAL_UPLOAD_BYTES:
                        raise ValueError("file too large (max 300MB per file)")
                    out.write(chunk)
        except Exception as e:
            if os.path.isfile(dest_path):
                os.remove(dest_path)
            return utils.get_response(400, message=f"upload failed for {file.filename}: {e}")

        uploaded.append({
            "rel_path": f"{uid}/{material_id}{ext}",
            "filename": file.filename,
            "kind": "image" if ext in _MATERIAL_IMAGE_EXTS else "video",
        })
    return utils.get_response(200, {"materials": uploaded})


# --------------------------------------------------------------------------- #
# Long-form clipping: upload a video, AI picks highlight segments, queue one
# render job per chosen segment (each flows through the normal job pipeline).
# --------------------------------------------------------------------------- #
ALLOWED_CLIP_UPLOAD_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
MAX_CLIP_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2GB
MIN_CLIP_SOURCE_SECONDS = 20


@router.post("/saas/clips/upload", summary="Upload a long-form video to clip into Shorts")
def upload_clip_source(request: Request, file: UploadFile = File(...)):
    uid = _uid(request)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_CLIP_UPLOAD_EXTS:
        return utils.get_response(400, message=f"unsupported file type: {ext or 'unknown'}")

    upload_id = utils.get_uuid()
    user_dir = os.path.join(saas.uploads_dir(), uid)
    os.makedirs(user_dir, exist_ok=True)
    dest_path = os.path.join(user_dir, f"{upload_id}{ext}")

    size = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_CLIP_UPLOAD_BYTES:
                    raise ValueError("file too large (max 2GB)")
                out.write(chunk)
    except Exception as e:
        if os.path.isfile(dest_path):
            os.remove(dest_path)
        return utils.get_response(400, message=f"upload failed: {e}")

    try:
        duration = clips.probe_duration(dest_path)
    except Exception as e:
        os.remove(dest_path)
        return utils.get_response(400, message=f"could not read video: {e}")

    if duration < MIN_CLIP_SOURCE_SECONDS:
        os.remove(dest_path)
        return utils.get_response(400, message=f"video is too short to clip (minimum {MIN_CLIP_SOURCE_SECONDS}s)")

    _CLIP_UPLOADS[upload_id] = {
        "uid": uid, "path": dest_path, "duration": duration, "filename": file.filename,
    }
    return utils.get_response(200, {"upload_id": upload_id, "filename": file.filename, "duration": duration})


class ClipAnalyzeBody(BaseModel):
    upload_id: str
    clip_count: Optional[int] = 3


@router.post("/saas/clips/analyze", summary="Transcribe an uploaded video and pick highlight segments")
def analyze_clip_source(request: Request, body: ClipAnalyzeBody):
    uid = _uid(request)
    info = _CLIP_UPLOADS.get(body.upload_id)
    if not info or info["uid"] != uid:
        return utils.get_response(404, message="upload not found - please upload the video again")

    clip_count = max(1, min(int(body.clip_count or 3), 10))
    work_dir = os.path.join(os.path.dirname(info["path"]), f"{body.upload_id}-work")
    try:
        transcript_segments = clips.transcribe_source(info["path"], work_dir)
    except Exception as e:  # noqa: BLE001 - clipping still works without captions/AI selection
        logger.warning(f"transcription failed for upload {body.upload_id}: {e}")
        transcript_segments = []
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    segments = None
    if transcript_segments:
        try:
            with saas._user_config_scope(uid):
                transcript_text = clips.transcript_for_prompt(transcript_segments)
            segments = llm.generate_highlight_segments(transcript_text, clip_count, info["duration"])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"highlight selection failed for upload {body.upload_id}, using even spacing: {e}")
            segments = None

    if not segments:
        segments = clips.evenly_spaced_segments(info["duration"], clip_count)

    info["transcript_segments"] = transcript_segments
    info["segments"] = segments
    return utils.get_response(
        200, {"upload_id": body.upload_id, "segments": segments, "ai_selected": bool(transcript_segments)}
    )


class ClipQueueBody(BaseModel):
    upload_id: str
    segments: Optional[list] = None  # lets the user edit start/end/title before queuing


@router.post("/saas/clips/jobs", summary="Queue render jobs for the chosen highlight segments")
def queue_clip_jobs(request: Request, body: ClipQueueBody):
    if (denied := _blocked(request, "can_clip", "Clipping")) is not None:
        return denied
    uid = _uid(request)
    info = _CLIP_UPLOADS.get(body.upload_id)
    if not info or info["uid"] != uid:
        return utils.get_response(404, message="upload not found - please upload the video again")

    segments = body.segments if body.segments else info.get("segments")
    if not segments:
        return utils.get_response(400, message="no segments to queue - analyze the video first")

    cleaned = []
    for seg in segments:
        try:
            start, end = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start or start < 0 or end > info["duration"] + 1:
            continue
        cleaned.append({"start": start, "end": end, "title": (seg.get("title") or "Highlight clip").strip()[:100]})
    if not cleaned:
        return utils.get_response(400, message="no valid segments to queue")

    jobs = saas.queue_clip_jobs(uid, info["path"], cleaned, info.get("transcript_segments") or [])
    _CLIP_UPLOADS.pop(body.upload_id, None)
    return utils.get_response(200, {"jobs": jobs})


# --------------------------------------------------------------------------- #
# AI helper
# --------------------------------------------------------------------------- #
class GenScriptBody(BaseModel):
    video_subject: str
    video_language: Optional[str] = ""
    paragraph_number: Optional[int] = 1


@router.post("/saas/generate-script", summary="AI-generate a script, branded for this user's business")
def generate_script(request: Request, body: GenScriptBody):
    uid = _uid(request)
    profile = firestore_db.get_user_profile(uid)
    script_prompt = saas.SCRIPT_LENGTH_PROMPT
    business_context = saas._business_context_prompt(profile)
    if business_context:
        script_prompt = script_prompt + " " + business_context
    try:
        with saas._user_config_scope(uid):
            script = llm.generate_script(
                video_subject=body.video_subject,
                language=body.video_language or "",
                paragraph_number=body.paragraph_number or saas.DEFAULT_PARAGRAPHS,
                video_script_prompt=script_prompt,
            )
    except Exception as e:
        return utils.get_response(500, message=f"script generation failed: {e}")
    if not script or "Error: " in str(script):
        return utils.get_response(500, message=str(script) or "empty script")
    return utils.get_response(200, {"video_script": script})
