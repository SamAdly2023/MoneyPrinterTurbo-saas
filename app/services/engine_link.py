"""Server side of the downloadable Vidzy Engine - lets a user's own computer do
the video rendering instead of this (shared, resource-capped) host.

Flow:
    1. The user generates an Engine key in the dashboard (issue_token).
    2. The Engine app sends heartbeats and polls /api/v1/engine/v1/claim.
    3. While a user's Engine is online and enabled, that user's new stock-footage
       jobs are queued as "engine_pending" instead of "pending". The server's own
       render workers only ever claim "pending", so they never touch these.
    4. The Engine renders, uploads the MP4, and the server finishes the job
       exactly as if it had rendered it (publish metadata, auto-publish).
    5. If the Engine goes offline or stalls, sweep_user() hands its jobs back to
       the server queue - a user's video is never stranded on a closed laptop.

Auth is a signed token (itsdangerous, same secret as the session cookie but a
different salt) carrying just {uid, version}. Bumping `engine_version` on the
profile revokes every token issued before it, so no extra storage or DB-backend
changes are needed.

Credits are unchanged: reserved when the job is queued, refunded if it fails.
"""

import threading
from datetime import datetime, timedelta, timezone

from itsdangerous import BadSignature, URLSafeSerializer
from loguru import logger

from app.services import firestore_db

TOKEN_PREFIX = "vde_"

STATUS_ENGINE_PENDING = "engine_pending"
STATUS_ENGINE_PROCESSING = "engine_processing"

# The Engine heartbeats about every 30s; allow two missed beats plus slack.
ONLINE_WINDOW_SECONDS = 90
# A job the Engine claimed but hasn't reported progress on for this long is
# presumed lost (crash, sleep, closed lid) and goes back to the server queue.
STALL_SECONDS = 20 * 60

# Sources the Engine can render with only what the server hands it per job.
# "ai"/"avatar" need server-side paid APIs, "local" needs files on this server.
ENGINE_SOURCES = {"pexels", "pixabay"}

_claim_lock = threading.Lock()


def _serializer() -> URLSafeSerializer:
    from app.services.auth import _get_session_secret

    return URLSafeSerializer(_get_session_secret(), salt="vidzy-engine")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Tokens / device state (stored on the user's profile)
# --------------------------------------------------------------------------- #
def issue_token(uid: str, name: str = "") -> str:
    """Create a fresh Engine key, revoking any earlier one. Shown to the user once."""
    profile = firestore_db.get_user_profile(uid)
    version = int(profile.get("engine_version") or 0) + 1
    firestore_db.save_user_profile(uid, {
        "engine_version": version,
        "engine_prefer": True,
        "engine": {"name": (name or "My computer")[:60], "created_at": _now().isoformat(),
                   "last_seen": "", "app_version": "", "platform": ""},
    })
    return TOKEN_PREFIX + _serializer().dumps({"u": uid, "v": version})


def verify_token(raw: str):
    """Return the uid a valid, un-revoked Engine key belongs to, else None."""
    raw = (raw or "").strip()
    if not raw.startswith(TOKEN_PREFIX):
        return None
    try:
        payload = _serializer().loads(raw[len(TOKEN_PREFIX):])
    except BadSignature:
        return None
    uid = payload.get("u")
    if not uid:
        return None
    user = firestore_db.get_user(uid)
    if not user or user.get("is_disabled"):
        return None
    profile = firestore_db.get_user_profile(uid)
    if int(profile.get("engine_version") or 0) != payload.get("v") or not profile.get("engine"):
        return None
    return uid


def revoke(uid: str) -> None:
    profile = firestore_db.get_user_profile(uid)
    firestore_db.save_user_profile(uid, {
        "engine_version": int(profile.get("engine_version") or 0) + 1,
        "engine": {},
        "engine_prefer": False,
    })
    sweep_user(uid, force=True)


def set_prefer(uid: str, enabled: bool) -> None:
    firestore_db.save_user_profile(uid, {"engine_prefer": bool(enabled)})
    if not enabled:
        sweep_user(uid, force=True)


def heartbeat(uid: str, info: dict) -> None:
    profile = firestore_db.get_user_profile(uid)
    engine = dict(profile.get("engine") or {})
    engine.update({
        "last_seen": _now().isoformat(),
        "app_version": str(info.get("version") or "")[:30],
        "platform": str(info.get("platform") or "")[:60],
    })
    firestore_db.save_user_profile(uid, {"engine": engine})


def is_online(profile: dict) -> bool:
    last = _parse((profile.get("engine") or {}).get("last_seen"))
    return bool(last and (_now() - last).total_seconds() <= ONLINE_WINDOW_SECONDS)


def public_state(profile: dict) -> dict:
    engine = profile.get("engine") or {}
    return {
        "paired": bool(engine),
        "online": is_online(profile),
        "enabled": bool(profile.get("engine_prefer")),
        "name": engine.get("name", ""),
        "last_seen": engine.get("last_seen", ""),
        "app_version": engine.get("app_version", ""),
        "platform": engine.get("platform", ""),
    }


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def can_render_on_engine(params: dict) -> bool:
    """Only jobs whose inputs the Engine can obtain by itself."""
    if (params.get("video_source") or "pexels") not in ENGINE_SOURCES:
        return False
    if params.get("logo_path") or params.get("avatar_photo_path"):
        return False
    if params.get("video_materials"):
        return False
    return True


def should_route(uid: str, params: dict) -> bool:
    try:
        profile = firestore_db.get_user_profile(uid)
        return bool(profile.get("engine_prefer")) and is_online(profile) and can_render_on_engine(params)
    except Exception as e:  # noqa: BLE001 - routing must never block queueing a job
        logger.warning(f"engine routing check failed for {uid}: {e}")
        return False


# --------------------------------------------------------------------------- #
# Job lifecycle
# --------------------------------------------------------------------------- #
def claim(uid: str):
    """Hand the Engine this user's oldest waiting job, or None."""
    from app.services import saas

    with _claim_lock:
        waiting = [j for j in firestore_db.list_jobs(uid)
                   if j.get("status") == STATUS_ENGINE_PENDING and j.get("kind", "generate") == "generate"]
        if not waiting:
            return None
        job = min(waiting, key=lambda j: j.get("created_at", ""))
        nonce = saas.utils.get_uuid(remove_hyphen=True)
        saas.store.update(uid, job["id"], status=STATUS_ENGINE_PROCESSING, progress=1,
                          claimed_by="engine", claim_nonce=nonce, error="")
        # Passenger may run several worker processes, and _claim_lock only
        # covers this one - re-read to confirm the claim is really ours.
        fresh = saas.store.get(uid, job["id"])
        if not fresh or fresh.get("claim_nonce") != nonce:
            return None
        return fresh


def authorize_job(uid: str, job_id: str):
    """The job, only if it is currently out on the Engine; otherwise None."""
    from app.services import saas

    job = saas.store.get(uid, job_id)
    if job and job.get("status") == STATUS_ENGINE_PROCESSING:
        return job
    return None


def sweep_user(uid: str, force: bool = False) -> int:
    """Return this user's stranded Engine jobs to the server queue.

    A job is stranded when the Engine is offline (or switched off) or has
    gone quiet on a claimed job for STALL_SECONDS. Returns how many moved."""
    from app.services import saas

    profile = firestore_db.get_user_profile(uid)
    if not force and not profile.get("engine"):
        return 0  # never paired - nothing can be stranded (this runs on every dashboard poll)
    online = is_online(profile) and bool(profile.get("engine_prefer"))
    moved = 0
    for job in firestore_db.list_jobs(uid):
        status = job.get("status")
        if status not in (STATUS_ENGINE_PENDING, STATUS_ENGINE_PROCESSING):
            continue
        stalled = False
        if status == STATUS_ENGINE_PROCESSING:
            last = _parse(job.get("updated_at"))
            stalled = bool(last and (_now() - last).total_seconds() > STALL_SECONDS)
        if online and not force and not stalled:
            continue
        saas.store.update(uid, job["id"], status=saas.STATUS_PENDING, progress=0, claimed_by="",
                          claim_nonce="", fallback_reason="engine offline" if not stalled else "engine stalled")
        moved += 1
    if moved:
        logger.info(f"engine: returned {moved} job(s) for {uid} to the server queue")
        saas.engine.wake()
    return moved


def sweep_if_offline(uid: str, profile: dict) -> None:
    """Cheap hook for the 30s watchdog: only does work for a user who has an
    Engine paired but not currently online, and only once per offline spell."""
    engine = profile.get("engine") or {}
    if not engine or is_online(profile):
        return
    marker = engine.get("last_seen") or "never"
    if engine.get("swept_for") == marker:
        return
    sweep_user(uid)
    engine = dict(firestore_db.get_user_profile(uid).get("engine") or {})
    if engine:
        engine["swept_for"] = marker
        firestore_db.save_user_profile(uid, {"engine": engine})
