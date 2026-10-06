"""
API for the downloadable Vidzy Engine (see app/services/engine_link.py).

Two audiences, two kinds of auth:

  Dashboard (session cookie, like every other /saas route)
    GET    /saas/engine/device          pairing + online state for this user
    POST   /saas/engine/device/key      generate an Engine key (shown once)
    POST   /saas/engine/device/enabled  turn Engine rendering on/off
    DELETE /saas/engine/device          revoke the key / unpair

  The Engine app (Authorization: Bearer vde_...)
    POST   /engine/v1/heartbeat         "I'm online" + version info
    POST   /engine/v1/claim             take this user's next waiting job
    POST   /engine/v1/jobs/{id}/progress
    POST   /engine/v1/jobs/{id}/complete   multipart: the finished MP4
    POST   /engine/v1/jobs/{id}/fail       report a failure (job falls back to the server)

The /engine/ prefix is exempted from the session-cookie middleware in
app/asgi.py and authenticates itself here, the same arrangement as
/external/ (controllers/v1/external.py).
"""

import os
from typing import Optional

from fastapi import File, Form, Request, UploadFile
from loguru import logger
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.controllers.v1.base import new_router
from app.models.schema import VideoParams
from app.services import engine_link, firestore_db, saas
from app.services import task as tm
from app.utils import utils

router = new_router()

# Oldest Engine build the server still talks to; lets us force an upgrade if
# the job format ever changes incompatibly.
MIN_ENGINE_VERSION = "0.1.0"
# How often an idle Engine checks in. Every Engine online is one request per
# interval against a shared host, so keep this generous.
POLL_SECONDS = 30
# Finished-video upload cap. The shared host rejects very large request bodies
# outright, so say so clearly instead of letting the Engine see a bare 500.
MAX_UPLOAD_BYTES = 140 * 1024 * 1024


def _uid(request: Request) -> str:
    return request.state.user["uid"]


# --------------------------------------------------------------------------- #
# Dashboard side
# --------------------------------------------------------------------------- #
class KeyBody(BaseModel):
    name: Optional[str] = ""


class EnabledBody(BaseModel):
    enabled: bool


@router.get("/saas/engine/device", summary="This user's Engine pairing + online state")
def device_state(request: Request):
    uid = _uid(request)
    engine_link.sweep_user(uid)  # a dashboard poll is a free chance to rescue stranded jobs
    return utils.get_response(200, engine_link.public_state(firestore_db.get_user_profile(uid)))


@router.post("/saas/engine/device/key", summary="Generate an Engine key (replaces any earlier one)")
def device_key(request: Request, body: KeyBody):
    uid = _uid(request)
    key = engine_link.issue_token(uid, body.name or "")
    state = engine_link.public_state(firestore_db.get_user_profile(uid))
    state["key"] = key  # the only time it is ever shown
    return utils.get_response(200, state)


@router.post("/saas/engine/device/enabled", summary="Turn Engine rendering on or off")
def device_enabled(request: Request, body: EnabledBody):
    uid = _uid(request)
    engine_link.set_prefer(uid, body.enabled)
    return utils.get_response(200, engine_link.public_state(firestore_db.get_user_profile(uid)))


@router.delete("/saas/engine/device", summary="Revoke the Engine key")
def device_revoke(request: Request):
    uid = _uid(request)
    engine_link.revoke(uid)
    return utils.get_response(200, engine_link.public_state(firestore_db.get_user_profile(uid)))


# --------------------------------------------------------------------------- #
# Engine side
# --------------------------------------------------------------------------- #
def _engine_uid(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return engine_link.verify_token(raw)


_DENIED = "invalid or revoked Engine key - generate a new one in the dashboard"


class HeartbeatBody(BaseModel):
    version: Optional[str] = ""
    platform: Optional[str] = ""


@router.post("/engine/v1/heartbeat", summary="Engine check-in")
def heartbeat(request: Request, body: HeartbeatBody):
    uid = _engine_uid(request)
    if not uid:
        return utils.get_response(401, message=_DENIED)
    engine_link.heartbeat(uid, body.model_dump())
    user = firestore_db.get_user(uid) or {}
    profile = firestore_db.get_user_profile(uid)
    enabled = bool(profile.get("engine_prefer"))
    return utils.get_response(200, {
        "ok": True,
        "email": user.get("email", ""),
        "enabled": enabled,
        "min_engine_version": MIN_ENGINE_VERSION,
        # Jobs waiting for this Engine. It only calls /claim when this is > 0,
        # and the server - not the client - decides how often to check in, so
        # polling load on the shared host can be tuned without a new release.
        "pending": engine_link.pending_count(uid) if enabled else 0,
        "poll_seconds": POLL_SECONDS,
    })


def _job_settings(uid: str) -> dict:
    """The only server-side config the Engine gets: footage source keys and the
    look-and-feel defaults. LLM / publishing credentials never leave the server -
    the script and search terms are resolved here before the job is handed over."""
    g = firestore_db.get_global_settings()

    def _as_list(v):
        return [v] if isinstance(v, str) and v else (v if isinstance(v, list) else [])

    return {
        "video_source": g.get("video_source", "pexels"),
        "pexels_api_keys": _as_list(g.get("pexels_api_keys")),
        "pixabay_api_keys": _as_list(g.get("pixabay_api_keys")),
        "ui": {k: g[k] for k in saas.UI_KEYS if k in g},
    }


def _resolve_script_and_terms(uid: str, job: dict) -> dict:
    """Make sure the params carry a script and search terms - generating them
    here (server-side LLM keys) when the user only supplied a subject."""
    params = dict(job["params"])
    if params.get("video_script") and params.get("video_terms"):
        return params
    vp = VideoParams(**params)
    with saas._user_config_scope(uid):
        result = tm.start(task_id=utils.get_uuid(), params=vp, stop_at="terms")
    if not result or not result.get("script"):
        raise RuntimeError("could not generate a script for this video")
    params["video_script"] = result["script"]
    terms = result.get("terms") or []
    params["video_terms"] = ", ".join(terms) if isinstance(terms, list) else terms
    saas.store.update(uid, job["id"], params=params)
    return params


@router.post("/engine/v1/claim", summary="Take the next waiting job")
def claim(request: Request):
    uid = _engine_uid(request)
    if not uid:
        return utils.get_response(401, message=_DENIED)
    job = engine_link.claim(uid)
    if not job:
        return utils.get_response(200, {"job": None})
    try:
        params = _resolve_script_and_terms(uid, job)
    except Exception as e:  # noqa: BLE001 - hand the job back rather than strand it
        logger.error(f"engine claim: couldn't prepare job {job['id']}: {e}")
        saas.store.update(uid, job["id"], status=saas.STATUS_PENDING, progress=0, claimed_by="",
                          claim_nonce="", fallback_reason=f"engine prep failed: {e}")
        saas.engine.wake()
        return utils.get_response(200, {"job": None})
    return utils.get_response(200, {"job": {
        "id": job["id"],
        "title": job.get("title", ""),
        "params": params,
        "settings": _job_settings(uid),
        "claim_nonce": job["claim_nonce"],
    }})


class ProgressBody(BaseModel):
    progress: int
    claim_nonce: str


@router.post("/engine/v1/jobs/{job_id}/progress", summary="Report render progress")
def progress(request: Request, job_id: str, body: ProgressBody):
    uid = _engine_uid(request)
    if not uid:
        return utils.get_response(401, message=_DENIED)
    job = engine_link.authorize_job(uid, job_id)
    if not job or job.get("claim_nonce") != body.claim_nonce:
        return utils.get_response(409, message="this job is no longer assigned to this Engine")
    saas.store.update(uid, job_id, progress=max(1, min(94, int(body.progress))))
    return utils.get_response(200, {"ok": True})


class FailBody(BaseModel):
    error: str
    claim_nonce: str


@router.post("/engine/v1/jobs/{job_id}/fail", summary="Report a failed render")
def fail(request: Request, job_id: str, body: FailBody):
    uid = _engine_uid(request)
    if not uid:
        return utils.get_response(401, message=_DENIED)
    job = engine_link.authorize_job(uid, job_id)
    if not job or job.get("claim_nonce") != body.claim_nonce:
        return utils.get_response(409, message="this job is no longer assigned to this Engine")
    # Not the user's fault if their computer couldn't render it - let the server
    # try. If the server fails too, its own failure path refunds the credit.
    logger.warning(f"engine job {job_id} failed on the user's machine: {body.error[:300]}")
    saas.store.update(uid, job_id, status=saas.STATUS_PENDING, progress=0, claimed_by="", claim_nonce="",
                      fallback_reason="engine failed", engine_error=body.error[:500])
    saas.engine.wake()
    return utils.get_response(200, {"ok": True, "requeued_on_server": True})


@router.post("/engine/v1/jobs/{job_id}/complete", summary="Upload the finished video")
async def complete(
    request: Request,
    job_id: str,
    claim_nonce: str = Form(...),
    script: str = Form(""),
    file: UploadFile = File(...),
):
    uid = _engine_uid(request)
    if not uid:
        return utils.get_response(401, message=_DENIED)
    job = engine_link.authorize_job(uid, job_id)
    if not job or job.get("claim_nonce") != claim_nonce:
        return utils.get_response(409, message="this job is no longer assigned to this Engine")

    out_name = f"{job_id}_1.mp4"
    out_path = os.path.join(saas.output_dir(), out_name)
    tmp_path = out_path + ".part"
    written = 0
    try:
        with open(tmp_path, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise ValueError(f"video is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB upload limit")
                fh.write(chunk)
        if written < 10 * 1024:
            raise ValueError("uploaded file is empty or truncated")
        os.replace(tmp_path, out_path)
    except Exception as e:  # noqa: BLE001
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        logger.warning(f"engine upload for {job_id} rejected: {e}")
        return utils.get_response(400, message=str(e))

    final_script = script or job["params"].get("video_script", "")
    # Metadata generation calls an LLM - keep it off the event loop.
    await run_in_threadpool(saas.engine.finalize_job, uid, job, [f"/media/{out_name}"], final_script)
    saas.store.update(uid, job_id, rendered_on="engine")
    return utils.get_response(200, {"ok": True})
