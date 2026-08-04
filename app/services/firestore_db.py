"""Firestore data layer: per-user accounts, settings, social tokens, and jobs.

Schema:
  users/{uid}                  -> {email, provider, is_disabled, created_at,
                                    last_auto_generated_at, settings: {...}, social: {...}}
  users/{uid}/jobs/{job_id}     -> same job shape the engine has always used,
                                    just nested under its owner.

Everything here is uid-scoped by construction (the caller always supplies a
uid) except the `list_all_*`/`next_pending_job`/`reset_stuck_jobs` admin and
engine helpers, which intentionally cross users via collection-group queries.
"""

import datetime

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
    "business_bio": "",
    "auto_mode": False,
    "auto_publish": False,
    "auto_publish_platforms": [],
    "youtube_privacy": "public",
}

DEFAULT_GLOBAL_SETTINGS = {
    "llm_provider": "openai",
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


def create_user_if_missing(uid: str, email: str, provider: str) -> dict:
    ref = db.collection("users").document(uid)
    snap = ref.get()
    if snap.exists:
        data = snap.to_dict()
        data["uid"] = uid
        return data

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
    return doc


def list_users() -> list[dict]:
    out = []
    for snap in db.collection("users").stream():
        data = snap.to_dict()
        data["uid"] = snap.id
        out.append(data)
    return out


def set_user_disabled(uid: str, disabled: bool) -> None:
    db.collection("users").document(uid).update({"is_disabled": disabled})


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


def mark_auto_generated(uid: str) -> None:
    db.collection("users").document(uid).update({"last_auto_generated_at": _now()})


def next_auto_mode_user() -> dict | None:
    """Among users with auto_mode enabled and not disabled, pick the one
    who hasn't had a job auto-generated the longest (simple round robin)."""
    candidates = [
        u
        for u in list_users()
        if not u.get("is_disabled") and u.get("profile", {}).get("auto_mode")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda u: u.get("last_auto_generated_at") or "")
    return candidates[0]


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


def next_pending_job() -> tuple[str, dict] | None:
    """Oldest pending job across every user (fair global FIFO)."""
    query = (
        db.collection_group("jobs")
        .where(filter=FieldFilter("status", "==", "pending"))
        .order_by("created_at")
        .limit(1)
    )
    for snap in query.stream():
        data = snap.to_dict()
        uid = snap.reference.parent.parent.id
        return uid, data
    return None


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
