"""
Dashboard API for the local SaaS layer.

Endpoints (all under /api/v1):
    GET    /saas/settings              read editable settings
    POST   /saas/settings              persist settings to config.toml
    GET    /saas/jobs                  list all queued/finished jobs
    POST   /saas/jobs                  save a script and queue it
    DELETE /saas/jobs/{job_id}         remove a job (and its output copies)
    POST   /saas/jobs/{job_id}/retry   re-queue a failed/finished job
    GET    /saas/engine                engine status
    POST   /saas/engine/pause          pause the queue
    POST   /saas/engine/resume         resume the queue
    POST   /saas/generate-script       AI-generate a script (Groq/Grok/...)
"""

import os
from typing import Optional

from fastapi import Path, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import BaseModel

from app.config import config
from app.controllers.v1.base import new_router
from app.models.schema import VideoParams
from app.services import llm, publish, saas
from app.utils import utils

router = new_router()


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _first(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def _current_settings() -> dict:
    app = config.app
    ui = config.ui
    return {
        "video_source": app.get("video_source", "pexels"),
        "pexels_api_key": _first(app.get("pexels_api_keys", [])),
        "pixabay_api_key": _first(app.get("pixabay_api_keys", [])),
        "extra_token": app.get("extra_token", ""),
        "llm_provider": app.get("llm_provider", "groq"),
        "groq_api_key": app.get("groq_api_key", ""),
        "groq_model_name": app.get("groq_model_name", "llama-3.3-70b-versatile"),
        "grok_api_key": app.get("grok_api_key", ""),
        "grok_model_name": app.get("grok_model_name", "grok-4.3"),
        "openai_api_key": app.get("openai_api_key", ""),
        "openai_base_url": app.get("openai_base_url", ""),
        "openai_model_name": app.get("openai_model_name", "gpt-4o-mini"),
        # generation defaults
        "voice_name": ui.get("voice_name", "en-US-AndrewNeural-Male"),
        "video_aspect": ui.get("video_aspect", "9:16"),
        "subtitle_enabled": ui.get("subtitle_enabled", True),
        "font_size": ui.get("font_size", 60),
        "subtitle_position": ui.get("subtitle_position", "bottom"),
        "paragraph_number": ui.get("paragraph_number", 1),
        "video_clip_duration": ui.get("video_clip_duration", 5),
        "bgm_type": ui.get("bgm_type", "random"),
        # publishing
        "youtube_client_id": app.get("youtube_client_id", ""),
        "youtube_client_secret": app.get("youtube_client_secret", ""),
        "youtube_privacy": app.get("youtube_privacy", "public"),
        "tiktok_client_key": app.get("tiktok_client_key", ""),
        "tiktok_client_secret": app.get("tiktok_client_secret", ""),
        "publish_base_url": app.get("publish_base_url", "http://localhost:8080"),
        "auto_publish": app.get("auto_publish", False),
        "auto_publish_platforms": app.get("auto_publish_platforms", []),
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
    # publishing
    youtube_client_id: Optional[str] = None
    youtube_client_secret: Optional[str] = None
    youtube_privacy: Optional[str] = None
    tiktok_client_key: Optional[str] = None
    tiktok_client_secret: Optional[str] = None
    publish_base_url: Optional[str] = None
    auto_publish: Optional[bool] = None
    auto_publish_platforms: Optional[list] = None


_APP_STR_KEYS = {
    "video_source", "extra_token", "llm_provider",
    "groq_api_key", "groq_model_name", "grok_api_key", "grok_model_name",
    "openai_api_key", "openai_base_url", "openai_model_name",
    "youtube_client_id", "youtube_client_secret", "youtube_privacy",
    "tiktok_client_key", "tiktok_client_secret", "publish_base_url",
}
_APP_OTHER_KEYS = {"auto_publish", "auto_publish_platforms"}
_UI_KEYS = {
    "voice_name", "video_aspect", "subtitle_enabled", "font_size",
    "subtitle_position", "paragraph_number", "video_clip_duration", "bgm_type",
}


@router.get("/saas/settings", summary="Get dashboard settings")
def get_settings(request: Request):
    return utils.get_response(200, _current_settings())


@router.post("/saas/settings", summary="Save dashboard settings")
def save_settings(request: Request, body: SettingsBody):
    data = body.model_dump(exclude_none=True)

    # API keys are stored as rotating lists in the engine.
    if "pexels_api_key" in data:
        key = data.pop("pexels_api_key").strip()
        config.app["pexels_api_keys"] = [key] if key else []
    if "pixabay_api_key" in data:
        key = data.pop("pixabay_api_key").strip()
        config.app["pixabay_api_keys"] = [key] if key else []

    for k, v in data.items():
        if k in _APP_STR_KEYS or k in _APP_OTHER_KEYS:
            config.app[k] = v
        elif k in _UI_KEYS:
            config.ui[k] = v

    config.save_config()
    logger.info("settings saved")
    return utils.get_response(200, _current_settings())


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


def _build_params(body: JobBody) -> dict:
    ui = config.ui
    app = config.app
    raw = {
        "video_subject": body.video_subject.strip(),
        "video_script": (body.video_script or "").strip(),
        "video_terms": (body.video_terms or "").strip() or None,
        "video_source": body.video_source or app.get("video_source", "pexels"),
        "video_aspect": body.video_aspect or ui.get("video_aspect", "9:16"),
        "voice_name": body.voice_name or ui.get("voice_name", "en-US-AndrewNeural-Male"),
        "subtitle_enabled": ui.get("subtitle_enabled", True) if body.subtitle_enabled is None else body.subtitle_enabled,
        "video_clip_duration": body.video_clip_duration or ui.get("video_clip_duration", 5),
        "paragraph_number": body.paragraph_number or ui.get("paragraph_number", saas.DEFAULT_PARAGRAPHS),
        "video_count": body.video_count or 1,
        "bgm_type": body.bgm_type or ui.get("bgm_type", "random"),
        "font_size": body.font_size or ui.get("font_size", 60),
        "subtitle_position": body.subtitle_position or ui.get("subtitle_position", "bottom"),
        "font_name": ui.get("font_name", "MicrosoftYaHeiBold.ttc"),
        "text_fore_color": ui.get("text_fore_color", "#FFFFFF"),
        "video_script_prompt": saas.SCRIPT_LENGTH_PROMPT,
    }
    # Validate against the real schema; raises on bad input.
    return VideoParams(**raw).model_dump()


@router.get("/saas/jobs", summary="List all jobs")
def list_jobs(request: Request):
    jobs = saas.store.all()
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    for j in jobs:
        counts[j["status"]] = counts.get(j["status"], 0) + 1
    return utils.get_response(200, {"jobs": jobs, "counts": counts, "engine": saas.engine.status()})


@router.post("/saas/jobs", summary="Save a script and add it to the queue")
def create_job(request: Request, body: JobBody):
    if not body.video_subject.strip():
        return utils.get_response(400, message="video_subject is required")
    try:
        params = _build_params(body)
    except Exception as e:
        return utils.get_response(400, message=f"invalid parameters: {e}")
    job = saas.create_job(title=body.title or body.video_subject, params=params)
    return utils.get_response(200, job)


@router.delete("/saas/jobs/{job_id}", summary="Delete a job")
def delete_job(request: Request, job_id: str = Path(...)):
    job = saas.store.get(job_id)
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
    saas.store.delete(job_id)
    return utils.get_response(200, {"deleted": job_id})


@router.post("/saas/jobs/{job_id}/retry", summary="Re-queue a job")
def retry_job(request: Request, job_id: str = Path(...)):
    job = saas.store.get(job_id)
    if not job:
        return utils.get_response(404, message="job not found")
    saas.store.update(job_id, status=saas.STATUS_PENDING, progress=0, error="", videos=[])
    saas.engine.wake()
    return utils.get_response(200, saas.store.get(job_id))


# --------------------------------------------------------------------------- #
# Engine control
# --------------------------------------------------------------------------- #
@router.get("/saas/engine", summary="Engine status")
def engine_status(request: Request):
    return utils.get_response(200, saas.engine.status())


@router.post("/saas/engine/pause", summary="Pause the queue")
def engine_pause(request: Request):
    saas.engine.pause()
    return utils.get_response(200, saas.engine.status())


@router.post("/saas/engine/resume", summary="Resume the queue")
def engine_resume(request: Request):
    saas.engine.resume()
    return utils.get_response(200, saas.engine.status())


@router.post("/saas/engine/auto/start", summary="Start auto mode (AI keeps generating videos)")
def engine_auto_start(request: Request):
    saas.engine.auto_start()
    return utils.get_response(200, saas.engine.status())


@router.post("/saas/engine/auto/stop", summary="Stop auto mode")
def engine_auto_stop(request: Request):
    saas.engine.auto_stop()
    return utils.get_response(200, saas.engine.status())


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
    data = publish.status()
    data["settings"] = {
        "auto_publish": config.app.get("auto_publish", False),
        "auto_publish_platforms": config.app.get("auto_publish_platforms", []),
        "youtube_privacy": config.app.get("youtube_privacy", "public"),
        "publish_base_url": config.app.get("publish_base_url", "http://localhost:8080"),
    }
    return utils.get_response(200, data)


@router.get("/saas/{platform}/connect", summary="Get the OAuth URL for a platform")
def social_connect(request: Request, platform: str = Path(...)):
    try:
        if platform == "youtube":
            url = publish.youtube_auth_url()
        elif platform == "tiktok":
            url = publish.tiktok_auth_url()
        else:
            return utils.get_response(400, message="unknown platform")
    except ValueError as e:
        return utils.get_response(400, message=str(e))
    return utils.get_response(200, {"auth_url": url})


@router.get("/saas/youtube/callback", summary="YouTube OAuth callback")
def youtube_callback(request: Request, code: str = "", error: str = ""):
    if error or not code:
        return _popup_close_html(f"YouTube connection failed: {error or 'no code'}", ok=False)
    try:
        info = publish.youtube_exchange_code(code)
        return _popup_close_html(f"YouTube connected: {info.get('channel') or 'channel'}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"youtube callback failed: {e}")
        return _popup_close_html(f"YouTube connection failed: {e}", ok=False)


@router.get("/saas/tiktok/callback", summary="TikTok OAuth callback")
def tiktok_callback(request: Request, code: str = "", error: str = ""):
    if error or not code:
        return _popup_close_html(f"TikTok connection failed: {error or 'no code'}", ok=False)
    try:
        publish.tiktok_exchange_code(code)
        return _popup_close_html("TikTok connected")
    except Exception as e:  # noqa: BLE001
        logger.error(f"tiktok callback failed: {e}")
        return _popup_close_html(f"TikTok connection failed: {e}", ok=False)


@router.post("/saas/{platform}/disconnect", summary="Disconnect a platform")
def social_disconnect(request: Request, platform: str = Path(...)):
    if platform not in ("youtube", "tiktok"):
        return utils.get_response(400, message="unknown platform")
    publish.disconnect(platform)
    return utils.get_response(200, publish.status())


class PublishBody(BaseModel):
    platforms: Optional[list] = None


@router.post("/saas/jobs/{job_id}/publish", summary="Publish a finished job's video")
def publish_job(request: Request, job_id: str = Path(...), body: PublishBody = None):
    job = saas.store.get(job_id)
    if not job:
        return utils.get_response(404, message="job not found")
    if job["status"] != saas.STATUS_DONE or not job.get("videos"):
        return utils.get_response(400, message="job has no finished video to publish")

    platforms = (body.platforms if body else None) or ["youtube"]
    video_name = os.path.basename(job["videos"][0])
    video_path = os.path.join(saas.output_dir(), video_name)
    results = publish.publish_video(video_path, job.get("meta", {}), platforms)

    merged = dict(job.get("publish", {}))
    merged.update(results)
    saas.store.update(job_id, publish=merged)
    return utils.get_response(200, results)


# --------------------------------------------------------------------------- #
# AI helper
# --------------------------------------------------------------------------- #
class GenScriptBody(BaseModel):
    video_subject: str
    video_language: Optional[str] = ""
    paragraph_number: Optional[int] = 1


@router.post("/saas/generate-script", summary="AI-generate a script")
def generate_script(request: Request, body: GenScriptBody):
    try:
        script = llm.generate_script(
            video_subject=body.video_subject,
            language=body.video_language or "",
            paragraph_number=body.paragraph_number or saas.DEFAULT_PARAGRAPHS,
            video_script_prompt=saas.SCRIPT_LENGTH_PROMPT,
        )
    except Exception as e:
        return utils.get_response(500, message=f"script generation failed: {e}")
    if not script or "Error: " in str(script):
        return utils.get_response(500, message=str(script) or "empty script")
    return utils.get_response(200, {"video_script": script})
