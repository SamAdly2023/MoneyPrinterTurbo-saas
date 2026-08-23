"""Firestore data layer: per-user accounts, settings, social tokens, and jobs.

Schema:
  users/{uid}                  -> {email, provider, is_disabled, created_at,
                                    last_auto_generated_at, settings: {...}, social: {...}}
  users/{uid}/jobs/{job_id}     -> same job shape the engine has always used,
                                    just nested under its owner.

Everything here is uid-scoped by construction (the caller always supplies a
uid) except the `list_all_*`/`claim_next_pending_job`/`reset_stuck_jobs` admin
and engine helpers, which intentionally cross users via collection-group queries.
"""

import datetime

from loguru import logger

import firebase_admin.firestore  # noqa: F401 - ensures app is initialized first
from app.services import firebase_init  # noqa: F401
from google.cloud.firestore_v1.base_query import FieldFilter
from firebase_admin import firestore

db = firestore.client()

# Per-user data: business branding + this business's queue/publish toggles.
# API keys and technical integration config live in app_config/global instead
# (see DEFAULT_GLOBAL_SETTINGS below) - every signed-up user shares those.
DEFAULT_PROFILE = {
    "business_name": "",
    "business_address": "",
    "business_website": "",
    "business_email": "",
    "business_phone": "",
    "business_bio": "",
    "auto_mode": False,
    "auto_publish": False,
    "auto_publish_platforms": [],
    "youtube_privacy": "public",
    # Per-user video preferences, overriding the admin's platform defaults for
    # this account's own Auto Mode videos - "" / "default" means inherit.
    "video_aspect": "",
    "subtitle_position": "",
    "subtitle_pref": "default",  # "default" | "on" | "off"
    # Business logo watermark - logo_path is relative to saas.logos_dir()/{uid}/...
    "logo_path": "",
    "use_logo": False,
    # AI talking-avatar presenter photo (see app/services/avatar.py) - falls
    # back to the admin's platform-wide default photo when unset.
    "avatar_image_path": "",
}

DEFAULT_GLOBAL_SETTINGS = {
    "llm_provider": "openai",
    # Auto Mode jobs have no per-job override and inherit this shared flag
    # directly (see saas.py UI_KEYS / _user_config_scope) - it must default
    # to on so a fresh deployment (or a blank Settings-page save) never
    # silently ships every client's Auto Mode videos without captions.
    "subtitle_enabled": True,
}

_GLOBAL_SETTINGS_REF = ("app_config", "global")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_user(uid: str) -> dict | None:
    snap = db.collection("users").document(uid).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    data["uid"] = uid
    return data


def create_user_if_missing(uid: str, email: str, provider: str) -> tuple[dict, bool]:
    """Returns (user, is_new) - is_new is True only the very first time this
    uid signs in, so callers (e.g. the welcome email) never re-fire on a
    normal returning login."""
    ref = db.collection("users").document(uid)
    snap = ref.get()
    if snap.exists:
        data = snap.to_dict()
        data["uid"] = uid
        return data, False

    doc = {
        "email": email,
        "provider": provider,
        "is_disabled": False,
        "created_at": _now(),
        "last_auto_generated_at": None,
        "profile": dict(DEFAULT_PROFILE),
        "social": {},
    }
    ref.set(doc)
    doc["uid"] = uid
    return doc, True


def list_users() -> list[dict]:
    out = []
    for snap in db.collection("users").stream():
        data = snap.to_dict()
        data["uid"] = snap.id
        out.append(data)
    return out


def set_user_disabled(uid: str, disabled: bool) -> None:
    db.collection("users").document(uid).update({"is_disabled": disabled})


def set_user_admin(uid: str, is_admin: bool) -> None:
    db.collection("users").document(uid).update({"is_admin": is_admin})


# ---------------------------------------------------------------------------
# API keys (external platform access - see app/controllers/v1/external.py)
# ---------------------------------------------------------------------------
def set_user_api_key(uid: str, key_hash: str, prefix: str) -> None:
    db.collection("users").document(uid).update({
        "api_key_hash": key_hash,
        "api_key_prefix": prefix,
        "api_key_created_at": _now(),
    })


def clear_user_api_key(uid: str) -> None:
    db.collection("users").document(uid).update({
        "api_key_hash": firestore.DELETE_FIELD,
        "api_key_prefix": firestore.DELETE_FIELD,
        "api_key_created_at": firestore.DELETE_FIELD,
    })


def get_user_by_api_key_hash(key_hash: str) -> dict | None:
    query = db.collection("users").where(filter=FieldFilter("api_key_hash", "==", key_hash)).limit(1)
    for snap in query.stream():
        data = snap.to_dict()
        data["uid"] = snap.id
        return data
    return None


def get_user_profile(uid: str) -> dict:
    user = get_user(uid)
    profile = dict(DEFAULT_PROFILE)
    if user and user.get("profile"):
        profile.update(user["profile"])
    return profile


def save_user_profile(uid: str, profile: dict) -> None:
    db.collection("users").document(uid).set({"profile": profile}, merge=True)


def get_global_settings() -> dict:
    snap = db.collection(_GLOBAL_SETTINGS_REF[0]).document(_GLOBAL_SETTINGS_REF[1]).get()
    settings = dict(DEFAULT_GLOBAL_SETTINGS)
    if snap.exists:
        settings.update(snap.to_dict())
    return settings


def save_global_settings(settings: dict) -> None:
    db.collection(_GLOBAL_SETTINGS_REF[0]).document(_GLOBAL_SETTINGS_REF[1]).set(settings, merge=True)


def get_user_social(uid: str) -> dict:
    user = get_user(uid)
    return (user or {}).get("social", {}) or {}


def save_user_social(uid: str, platform: str, token_data: dict) -> None:
    db.collection("users").document(uid).set(
        {"social": {platform: token_data}}, merge=True
    )


def clear_user_social(uid: str, platform: str) -> None:
    db.collection("users").document(uid).update({f"social.{platform}": firestore.DELETE_FIELD})


def claim_next_auto_mode_user(worker_id: str) -> dict | None:
    """Atomically pick AND mark the eligible user who hasn't had a job
    auto-generated the longest (simple round robin).

    Picking the candidate and marking last_auto_generated_at used to be two
    separate steps done by the caller after generation - fine for a single
    worker, but two concurrent workers could both pick the same "most
    overdue" user before either mark landed, generating two jobs for them
    in one round. Folding the mark into the same transaction that claims the
    user closes that gap; the caller no longer needs to mark_auto_generated
    itself (matches prior behavior of marking even on generation failure -
    "still rotate past this user" - just done atomically up front instead).
    """
    candidates = [
        u
        for u in list_users()
        if not u.get("is_disabled") and u.get("profile", {}).get("auto_mode")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda u: u.get("last_auto_generated_at") or "")

    @firestore.transactional
    def _claim(transaction, ref):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict()
        if not data or data.get("is_disabled") or not data.get("profile", {}).get("auto_mode"):
            return None
        transaction.update(ref, {"last_auto_generated_at": _now()})
        return data

    for u in candidates[:5]:
        ref = db.collection("users").document(u["uid"])
        data = _claim(db.transaction(), ref)
        if data is not None:
            data["uid"] = u["uid"]
            return data
    return None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def _jobs_ref(uid: str):
    return db.collection("users").document(uid).collection("jobs")


def create_job(uid: str, job: dict) -> dict:
    job = dict(job)
    job.setdefault("created_at", _now())
    job.setdefault("updated_at", job["created_at"])
    job_id = job["id"]
    _jobs_ref(uid).document(job_id).set(job)
    return job


def get_job(uid: str, job_id: str) -> dict | None:
    snap = _jobs_ref(uid).document(job_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    data["uid"] = uid
    return data


def list_jobs(uid: str) -> list[dict]:
    out = []
    for snap in _jobs_ref(uid).order_by("created_at", direction="DESCENDING").stream():
        data = snap.to_dict()
        data["uid"] = uid
        out.append(data)
    return out


def list_all_jobs() -> list[dict]:
    """Admin-only: every job across every user, newest first."""
    out = []
    for snap in db.collection_group("jobs").stream():
        data = snap.to_dict()
        data["uid"] = snap.reference.parent.parent.id
        out.append(data)
    out.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return out


def update_job(uid: str, job_id: str, **changes) -> None:
    changes["updated_at"] = _now()
    _jobs_ref(uid).document(job_id).update(changes)


def delete_job(uid: str, job_id: str) -> None:
    _jobs_ref(uid).document(job_id).delete()


def claim_next_pending_job(worker_id: str) -> tuple[str, dict] | None:
    """Atomically claim the oldest pending job (fair global FIFO).

    Reading the oldest pending job and separately marking it 'processing'
    would race once more than one worker (thread or Cloud Run instance) can
    poll at the same time - two workers could both see the same job as
    pending and both start rendering it. This does the read-and-claim as one
    Firestore transaction per candidate, so only one worker ever wins a given
    job; looks at a small batch in case the top candidate gets claimed by
    another worker between the initial query and this worker's attempt.
    """
    query = (
        db.collection_group("jobs")
        .where(filter=FieldFilter("status", "==", "pending"))
        .order_by("created_at")
        .limit(5)
    )
    candidates = list(query.stream())

    @firestore.transactional
    def _claim(transaction, ref):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict()
        if not data or data.get("status") != "pending":
            return None
        transaction.update(ref, {"status": "processing", "claimed_by": worker_id, "updated_at": _now()})
        return data

    for snap in candidates:
        ref = snap.reference
        data = _claim(db.transaction(), ref)
        if data is not None:
            data["status"] = "processing"
            uid = ref.parent.parent.id
            return uid, data
    return None


# ---------------------------------------------------------------------------
# Shared engine state (pause / global auto-mode kill switch)
# ---------------------------------------------------------------------------
# Previously these lived as in-memory threading.Event flags on a single
# Engine instance - safe only because exactly one process ever ran the
# engine. Now that multiple worker threads (and potentially multiple Cloud
# Run instances) each run their own Engine, an admin's pause/resume click
# must be visible to ALL of them, not just whichever one happened to receive
# that HTTP request.
_ENGINE_STATE_REF = ("app_config", "engine_state")


def get_engine_state() -> dict:
    snap = db.collection(_ENGINE_STATE_REF[0]).document(_ENGINE_STATE_REF[1]).get()
    data = snap.to_dict() if snap.exists else {}
    return {"paused": bool(data.get("paused", False)), "auto_killed": bool(data.get("auto_killed", False))}


def set_engine_state(**changes) -> None:
    db.collection(_ENGINE_STATE_REF[0]).document(_ENGINE_STATE_REF[1]).set(changes, merge=True)


_YOUTUBE_QUOTA_REF = ("app_config", "youtube_quota")


def reserve_youtube_upload_slot(daily_cap: int) -> bool:
    """Atomically claim one of today's shared YouTube upload slots.

    YouTube Data API quota (10,000 units/day by default, 1,600 units per
    upload = ~6 uploads/day) is scoped to the whole Cloud project, not per
    user - every connected channel across every business shares it. Without
    this, the daily cap gets hit mid-upload and YouTube returns a raw 429
    that surfaces as a confusing error on whatever job happens to be
    uploading at the time. daily_cap<=0 means uncapped (admin hasn't set one).
    """
    if daily_cap <= 0:
        return True
    today = _now()[:10]
    ref = db.collection(_YOUTUBE_QUOTA_REF[0]).document(_YOUTUBE_QUOTA_REF[1])

    @firestore.transactional
    def _claim(transaction):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() if snap.exists else {}
        count = data.get("count", 0) if data.get("date") == today else 0
        if count >= daily_cap:
            return False
        transaction.set(ref, {"date": today, "count": count + 1})
        return True

    return _claim(db.transaction())


def list_processing_jobs() -> list[dict]:
    """Every job actively rendering right now, across every worker/user.

    Polled every few seconds by every open dashboard (via Engine.status()),
    so a transient Firestore error here (e.g. a missing index on a fresh
    database) degrades to "nothing shown as processing" instead of taking
    down the whole status endpoint.
    """
    out = []
    try:
        for snap in db.collection_group("jobs").where(filter=FieldFilter("status", "==", "processing")).stream():
            data = snap.to_dict()
            data["uid"] = snap.reference.parent.parent.id
            out.append(data)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"list_processing_jobs failed: {e}")
    return out


# ---------------------------------------------------------------------------
# Invites (signup is invitation-only)
# ---------------------------------------------------------------------------
def create_invite(token: str, email: str, created_by: str) -> dict:
    doc = {
        "token": token,
        "email": (email or "").strip().lower(),
        "created_by": created_by,
        "created_at": _now(),
        "used_at": None,
        "used_by_uid": None,
    }
    db.collection("invites").document(token).set(doc)
    return doc


def get_invite(token: str) -> dict | None:
    snap = db.collection("invites").document(token).get()
    return snap.to_dict() if snap.exists else None


def claim_invite(token: str, email: str, uid: str) -> bool:
    """Single-use, and only by the address it was issued to.

    The read and the mark happen in one transaction so the same link opened
    twice at once cannot create two accounts.
    """
    email = (email or "").strip().lower()
    ref = db.collection("invites").document(token)

    @firestore.transactional
    def _claim(transaction):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() if snap.exists else None
        if not data or data.get("used_at") or (data.get("email") or "") != email:
            return False
        transaction.update(ref, {"used_at": _now(), "used_by_uid": uid})
        return True

    return _claim(db.transaction())


def list_invites() -> list[dict]:
    out = []
    for snap in db.collection("invites").stream():
        out.append(snap.to_dict())
    out.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    return out


def delete_invite(token: str) -> None:
    db.collection("invites").document(token).delete()


def set_user_features(uid: str, **flags) -> None:
    """Per-user capability switches (can_render, can_clip). Absent means
    allowed, so existing accounts keep working without a migration."""
    db.collection("users").document(uid).set(
        {k: bool(v) for k, v in flags.items()}, merge=True
    )


# ---------------------------------------------------------------------------
# Visitor analytics (public marketing-page traffic - admin-only to view)
# ---------------------------------------------------------------------------
def get_visitor_session(session_id: str) -> dict | None:
    snap = db.collection("visitor_sessions").document(session_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


def create_visitor_session(session_id: str, data: dict) -> None:
    data = dict(data)
    now = _now()
    data["first_seen"] = now
    data["last_seen"] = now
    data["pageview_count"] = 0
    db.collection("visitor_sessions").document(session_id).set(data)


def add_visitor_pageview(session_id: str, path: str, title: str) -> None:
    """Also bumps the session's pageview_count/last_seen - a plain
    read-then-write rather than an atomic increment, since a rare undercount
    from two pageviews landing in the same instant is harmless for a traffic
    dashboard and this avoids depending on the firestore.Increment surface."""
    db.collection("visitor_pageviews").add(
        {"session_id": session_id, "path": path, "title": title, "created_at": _now()}
    )
    ref = db.collection("visitor_sessions").document(session_id)
    snap = ref.get()
    count = (snap.to_dict() or {}).get("pageview_count", 0) if snap.exists else 0
    ref.update({"pageview_count": count + 1, "last_seen": _now()})


def list_visitor_sessions(limit: int = 500) -> list[dict]:
    out = []
    for snap in (
        db.collection("visitor_sessions")
        .order_by("last_seen", direction="DESCENDING")
        .limit(limit)
        .stream()
    ):
        data = snap.to_dict()
        data["session_id"] = snap.id
        out.append(data)
    return out


def reset_stuck_jobs() -> None:
    """Crash recovery: any job left 'processing' at startup goes back to pending.

    Filters in Python rather than via a Firestore query so this doesn't need
    its own collection-group index - it only runs once at startup and
    'processing' jobs are rare (at most one system-wide in steady state), so
    a full scan of the jobs collection group is cheap.
    """
    for snap in db.collection_group("jobs").stream():
        if snap.to_dict().get("status") == "processing":
            snap.reference.update({"status": "pending", "updated_at": _now()})
