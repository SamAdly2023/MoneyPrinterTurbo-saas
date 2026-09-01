"""SQLite data layer - the local app's private database.

Same 34 functions as db_firestore, same shapes in and out, so nothing above
this layer knows which one it is talking to (see firestore_db.py, the facade
that picks between them).

Why this exists: local and hosted were sharing one Firestore, so a video
rendered locally appeared in the live dashboard with a file nobody else could
play, and live jobs got claimed by whichever machine polled first. Auth stays
on Firebase in both modes - it is the *data* that needs to be separate.

Storage model mirrors Firestore's document shape rather than normalising it:
each row keeps the same dict under a `data` JSON column, with only the fields
the queue actually queries (status, created_at) promoted to real columns. That
keeps behaviour identical without a schema migration every time a job grows a
field.

The file lives outside the repo tree by default (storage/ is gitignored), so
editing code, reinstalling dependencies, or re-running the launcher never
touches it. Point MPT_SQLITE_PATH somewhere else to override.
"""

import json
import os
import sqlite3
import threading
import datetime

from loguru import logger

from app.config import config
from app.utils import utils

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
    "video_aspect": "",
    "subtitle_position": "",
    "subtitle_pref": "default",
    "logo_path": "",
    "use_logo": False,
    "avatar_image_path": "",
}

DEFAULT_GLOBAL_SETTINGS = {
    "llm_provider": "openai",
    "subtitle_enabled": True,
}

# Copied into the local database the first time it is created, so the local
# app starts with working API keys instead of a blank Settings page. After
# that the database wins - edits in Settings are not overwritten on restart.
_SEED_FROM_CONFIG = (
    "video_source", "llm_provider",
    "groq_api_key", "groq_model_name", "grok_api_key", "grok_model_name",
    "openai_api_key", "openai_base_url", "openai_model_name",
    "pexels_api_keys", "pixabay_api_keys",
    "youtube_client_id", "youtube_client_secret",
    "tiktok_client_key", "tiktok_client_secret",
    "facebook_app_id", "facebook_app_secret",
    "linkedin_client_id", "linkedin_client_secret",
    "publish_base_url", "subtitle_enabled",
)

_lock = threading.Lock()
_conn = None


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def db_path() -> str:
    override = os.getenv("MPT_SQLITE_PATH", "").strip()
    if override:
        return override
    return os.path.join(utils.storage_dir(create=True), "vidzy.db")


def _connect():
    global _conn
    if _conn is not None:
        return _conn
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # check_same_thread=False: the engine renders on worker threads while the
    # web server answers requests on others. Every write goes through _lock.
    _conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _init_schema(_conn)
    logger.success(f"local database: {path}")
    return _conn


def _init_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            uid  TEXT PRIMARY KEY,
            data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            uid        TEXT NOT NULL,
            job_id     TEXT NOT NULL,
            status     TEXT,
            created_at TEXT,
            data       TEXT NOT NULL,
            PRIMARY KEY (uid, job_id)
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
        CREATE TABLE IF NOT EXISTS app_config (
            key  TEXT PRIMARY KEY,
            data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invites (
            token       TEXT PRIMARY KEY,
            email       TEXT,
            created_by  TEXT,
            created_at  TEXT,
            used_at     TEXT,
            used_by_uid TEXT
        );
        CREATE TABLE IF NOT EXISTS visitor_sessions (
            session_id TEXT PRIMARY KEY,
            last_seen  TEXT,
            data       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS visitor_pageviews (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            path       TEXT,
            title      TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS processed_payments (
            payment_id TEXT PRIMARY KEY,
            uid        TEXT,
            credits    INTEGER,
            created_at TEXT
        );
        """
    )
    conn.commit()
    _seed_global_settings(conn)


def _seed_global_settings(conn):
    row = conn.execute("SELECT data FROM app_config WHERE key='global'").fetchone()
    if row is not None:
        return
    seed = dict(DEFAULT_GLOBAL_SETTINGS)
    for key in _SEED_FROM_CONFIG:
        value = config.app.get(key)
        if value not in (None, "", [], {}):
            seed[key] = value
    conn.execute(
        "INSERT INTO app_config(key, data) VALUES('global', ?)", (json.dumps(seed),)
    )
    conn.commit()
    logger.info(f"seeded local settings from config.toml ({len(seed)} keys)")


def _get_doc(table: str, key_col: str, key: str) -> dict | None:
    row = _connect().execute(
        f"SELECT data FROM {table} WHERE {key_col}=?", (key,)
    ).fetchone()
    return json.loads(row["data"]) if row else None


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def get_user(uid: str) -> dict | None:
    data = _get_doc("users", "uid", uid)
    if data is None:
        return None
    data["uid"] = uid
    return data


def create_user_if_missing(uid: str, email: str, provider: str) -> tuple[dict, bool]:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT data FROM users WHERE uid=?", (uid,)).fetchone()
        if row:
            data = json.loads(row["data"])
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
        conn.execute("INSERT INTO users(uid, data) VALUES(?, ?)", (uid, json.dumps(doc)))
        conn.commit()
        doc["uid"] = uid
        return doc, True


def list_users() -> list[dict]:
    out = []
    for row in _connect().execute("SELECT uid, data FROM users"):
        data = json.loads(row["data"])
        data["uid"] = row["uid"]
        out.append(data)
    return out


def _update_user(uid: str, changes: dict, delete_keys: tuple = ()) -> None:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT data FROM users WHERE uid=?", (uid,)).fetchone()
        if not row:
            return
        data = json.loads(row["data"])
        data.update(changes)
        for k in delete_keys:
            data.pop(k, None)
        conn.execute("UPDATE users SET data=? WHERE uid=?", (json.dumps(data), uid))
        conn.commit()


def set_user_disabled(uid: str, disabled: bool) -> None:
    _update_user(uid, {"is_disabled": disabled})


def set_user_admin(uid: str, is_admin: bool) -> None:
    _update_user(uid, {"is_admin": is_admin})


# ---------------------------------------------------------------------------
# Credits (live-site paywall - see app/services/billing.py)
# ---------------------------------------------------------------------------
def get_user_credits(uid: str) -> int:
    user = get_user(uid)
    return int((user or {}).get("credits", 0))


def reserve_credits(uid: str, count: int = 1) -> bool:
    """Atomically spend `count` credits if the balance covers it, all or
    nothing - used to reserve credits for a whole batch of clip jobs in one
    call rather than letting some clips queue and others get rejected
    mid-batch. BEGIN IMMEDIATE takes the write lock up front, matching
    reserve_youtube_upload_slot's pattern (a per-user document here, a
    shared counter document there)."""
    with _lock:
        conn = _connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT data FROM users WHERE uid=?", (uid,)).fetchone()
            if not row:
                conn.rollback()
                return False
            data = json.loads(row["data"])
            balance = int(data.get("credits", 0))
            if balance < count:
                conn.rollback()
                return False
            data["credits"] = balance - count
            conn.execute("UPDATE users SET data=? WHERE uid=?", (json.dumps(data), uid))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def refund_credits(uid: str, count: int = 1) -> None:
    """Called from the render failure path - a queued job reserves credits
    up front, and this gives them back if the render never delivered."""
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT data FROM users WHERE uid=?", (uid,)).fetchone()
        if not row:
            return
        data = json.loads(row["data"])
        data["credits"] = int(data.get("credits", 0)) + count
        conn.execute("UPDATE users SET data=? WHERE uid=?", (json.dumps(data), uid))
        conn.commit()


def add_credits(uid: str, count: int) -> None:
    """Plain grant - admin top-ups. The PayPal purchase path uses
    record_payment_if_new instead, which wraps this with idempotency."""
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT data FROM users WHERE uid=?", (uid,)).fetchone()
        if not row:
            return
        data = json.loads(row["data"])
        data["credits"] = int(data.get("credits", 0)) + count
        conn.execute("UPDATE users SET data=? WHERE uid=?", (json.dumps(data), uid))
        conn.commit()


def record_payment_if_new(payment_id: str, uid: str, count: int) -> bool:
    """Grants `count` credits for a PayPal payment exactly once.

    PayPal retries webhooks and a client can replay a capture call, so the
    insert-into-processed_payments and the credit grant happen in the same
    transaction: if payment_id is already present, this is a no-op and
    returns False - the caller (billing.py) uses that to tell an already-
    processed payment apart from a newly-applied one, without ever risking
    double-crediting the same payment.
    """
    with _lock:
        conn = _connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT 1 FROM processed_payments WHERE payment_id=?", (payment_id,)
            ).fetchone()
            if row:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO processed_payments(payment_id, uid, credits, created_at) VALUES(?,?,?,?)",
                (payment_id, uid, count, _now()),
            )
            urow = conn.execute("SELECT data FROM users WHERE uid=?", (uid,)).fetchone()
            if not urow:
                conn.rollback()
                return False
            data = json.loads(urow["data"])
            data["credits"] = int(data.get("credits", 0)) + count
            conn.execute("UPDATE users SET data=? WHERE uid=?", (json.dumps(data), uid))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def set_auto_mode_subscription(uid: str, active: bool) -> None:
    _update_user(uid, {"auto_mode_subscription_active": active})


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------
def set_user_api_key(uid: str, key_hash: str, prefix: str) -> None:
    _update_user(uid, {
        "api_key_hash": key_hash,
        "api_key_prefix": prefix,
        "api_key_created_at": _now(),
    })


def clear_user_api_key(uid: str) -> None:
    _update_user(uid, {}, delete_keys=("api_key_hash", "api_key_prefix", "api_key_created_at"))


def get_user_by_api_key_hash(key_hash: str) -> dict | None:
    for user in list_users():
        if user.get("api_key_hash") == key_hash:
            return user
    return None


def get_user_profile(uid: str) -> dict:
    user = get_user(uid)
    profile = dict(DEFAULT_PROFILE)
    if user and user.get("profile"):
        profile.update(user["profile"])
    return profile


def save_user_profile(uid: str, profile: dict) -> None:
    """Merges, matching Firestore's set(..., merge=True) on a nested map -
    keys the caller left out keep their existing values."""
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT data FROM users WHERE uid=?", (uid,)).fetchone()
        if not row:
            return
        data = json.loads(row["data"])
        merged = dict(data.get("profile") or {})
        merged.update(profile)
        data["profile"] = merged
        conn.execute("UPDATE users SET data=? WHERE uid=?", (json.dumps(data), uid))
        conn.commit()


def get_global_settings() -> dict:
    settings = dict(DEFAULT_GLOBAL_SETTINGS)
    stored = _get_doc("app_config", "key", "global")
    if stored:
        settings.update(stored)
    return settings


def save_global_settings(settings: dict) -> None:
    with _lock:
        conn = _connect()
        current = _get_doc("app_config", "key", "global") or {}
        current.update(settings)
        conn.execute(
            "INSERT INTO app_config(key, data) VALUES('global', ?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
            (json.dumps(current),),
        )
        conn.commit()


def get_user_social(uid: str) -> dict:
    user = get_user(uid)
    return (user or {}).get("social", {}) or {}


def save_user_social(uid: str, platform: str, token_data: dict) -> None:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT data FROM users WHERE uid=?", (uid,)).fetchone()
        if not row:
            return
        data = json.loads(row["data"])
        social = dict(data.get("social") or {})
        merged = dict(social.get(platform) or {})
        merged.update(token_data)
        social[platform] = merged
        data["social"] = social
        conn.execute("UPDATE users SET data=? WHERE uid=?", (json.dumps(data), uid))
        conn.commit()


def clear_user_social(uid: str, platform: str) -> None:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT data FROM users WHERE uid=?", (uid,)).fetchone()
        if not row:
            return
        data = json.loads(row["data"])
        social = dict(data.get("social") or {})
        social.pop(platform, None)
        data["social"] = social
        conn.execute("UPDATE users SET data=? WHERE uid=?", (json.dumps(data), uid))
        conn.commit()


def claim_next_auto_mode_user(worker_id: str) -> dict | None:
    """Pick and mark the eligible user who waited longest, in one transaction."""
    with _lock:
        conn = _connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            candidates = []
            for row in conn.execute("SELECT uid, data FROM users"):
                data = json.loads(row["data"])
                if data.get("is_disabled") or not (data.get("profile") or {}).get("auto_mode"):
                    continue
                data["uid"] = row["uid"]
                candidates.append(data)
            if not candidates:
                conn.rollback()
                return None
            candidates.sort(key=lambda u: u.get("last_auto_generated_at") or "")
            chosen = candidates[0]
            chosen["last_auto_generated_at"] = _now()
            stored = dict(chosen)
            uid = stored.pop("uid")
            conn.execute("UPDATE users SET data=? WHERE uid=?", (json.dumps(stored), uid))
            conn.commit()
            return chosen
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def create_job(uid: str, job: dict) -> dict:
    job = dict(job)
    job.setdefault("created_at", _now())
    job.setdefault("updated_at", job["created_at"])
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO jobs(uid, job_id, status, created_at, data) VALUES(?,?,?,?,?) "
            "ON CONFLICT(uid, job_id) DO UPDATE SET status=excluded.status, "
            "created_at=excluded.created_at, data=excluded.data",
            (uid, job["id"], job.get("status"), job["created_at"], json.dumps(job)),
        )
        conn.commit()
    return job


def get_job(uid: str, job_id: str) -> dict | None:
    row = _connect().execute(
        "SELECT data FROM jobs WHERE uid=? AND job_id=?", (uid, job_id)
    ).fetchone()
    if not row:
        return None
    data = json.loads(row["data"])
    data["uid"] = uid
    return data


def list_jobs(uid: str) -> list[dict]:
    out = []
    for row in _connect().execute(
        "SELECT data FROM jobs WHERE uid=? ORDER BY created_at DESC", (uid,)
    ):
        data = json.loads(row["data"])
        data["uid"] = uid
        out.append(data)
    return out


def list_all_jobs() -> list[dict]:
    out = []
    for row in _connect().execute("SELECT uid, data FROM jobs ORDER BY created_at DESC"):
        data = json.loads(row["data"])
        data["uid"] = row["uid"]
        out.append(data)
    return out


def update_job(uid: str, job_id: str, **changes) -> None:
    changes["updated_at"] = _now()
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT data FROM jobs WHERE uid=? AND job_id=?", (uid, job_id)
        ).fetchone()
        if not row:
            return
        data = json.loads(row["data"])
        data.update(changes)
        conn.execute(
            "UPDATE jobs SET status=?, data=? WHERE uid=? AND job_id=?",
            (data.get("status"), json.dumps(data), uid, job_id),
        )
        conn.commit()


def delete_job(uid: str, job_id: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM jobs WHERE uid=? AND job_id=?", (uid, job_id))
        conn.commit()


def claim_next_pending_job(worker_id: str) -> tuple[str, dict] | None:
    """Claim the oldest pending job. BEGIN IMMEDIATE takes the write lock up
    front, so two render threads can never claim the same job."""
    with _lock:
        conn = _connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT uid, job_id, data FROM jobs WHERE status='pending' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            data = json.loads(row["data"])
            data["status"] = "processing"
            data["claimed_by"] = worker_id
            data["updated_at"] = _now()
            conn.execute(
                "UPDATE jobs SET status='processing', data=? WHERE uid=? AND job_id=?",
                (json.dumps(data), row["uid"], row["job_id"]),
            )
            conn.commit()
            return row["uid"], data
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# Engine state
# ---------------------------------------------------------------------------
def get_engine_state() -> dict:
    data = _get_doc("app_config", "key", "engine_state") or {}
    return {
        "paused": bool(data.get("paused", False)),
        "auto_killed": bool(data.get("auto_killed", False)),
    }


def set_engine_state(**changes) -> None:
    with _lock:
        conn = _connect()
        current = _get_doc("app_config", "key", "engine_state") or {}
        current.update(changes)
        conn.execute(
            "INSERT INTO app_config(key, data) VALUES('engine_state', ?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
            (json.dumps(current),),
        )
        conn.commit()


def reserve_youtube_upload_slot(daily_cap: int) -> bool:
    if daily_cap <= 0:
        return True
    today = _now()[:10]
    with _lock:
        conn = _connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            data = _get_doc("app_config", "key", "youtube_quota") or {}
            count = data.get("count", 0) if data.get("date") == today else 0
            if count >= daily_cap:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO app_config(key, data) VALUES('youtube_quota', ?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (json.dumps({"date": today, "count": count + 1}),),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def list_processing_jobs() -> list[dict]:
    out = []
    try:
        for row in _connect().execute(
            "SELECT uid, data FROM jobs WHERE status='processing'"
        ):
            data = json.loads(row["data"])
            data["uid"] = row["uid"]
            out.append(data)
    except Exception as e:  # noqa: BLE001 - polled constantly; never break status
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
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO invites(token, email, created_by, created_at, used_at, used_by_uid) "
            "VALUES(?,?,?,?,NULL,NULL)",
            (doc["token"], doc["email"], doc["created_by"], doc["created_at"]),
        )
        conn.commit()
    return doc


def get_invite(token: str) -> dict | None:
    row = _connect().execute(
        "SELECT token, email, created_by, created_at, used_at, used_by_uid "
        "FROM invites WHERE token=?", (token,)
    ).fetchone()
    return dict(row) if row else None


def claim_invite(token: str, email: str, uid: str) -> bool:
    """Single-use, and only by the address it was issued to.

    Both checks and the write happen inside one BEGIN IMMEDIATE, so two
    simultaneous sign-ups with the same link cannot both succeed.
    """
    email = (email or "").strip().lower()
    with _lock:
        conn = _connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT email, used_at FROM invites WHERE token=?", (token,)
            ).fetchone()
            if not row or row["used_at"] or row["email"] != email:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE invites SET used_at=?, used_by_uid=? WHERE token=?",
                (_now(), uid, token),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def list_invites() -> list[dict]:
    return [
        dict(row)
        for row in _connect().execute(
            "SELECT token, email, created_by, created_at, used_at, used_by_uid "
            "FROM invites ORDER BY created_at DESC"
        )
    ]


def delete_invite(token: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM invites WHERE token=?", (token,))
        conn.commit()


def set_user_features(uid: str, **flags) -> None:
    """Per-user capability switches (can_render, can_clip). Absent means
    allowed, so existing accounts keep working without a migration."""
    _update_user(uid, {k: bool(v) for k, v in flags.items()})


# ---------------------------------------------------------------------------
# Visitor analytics
# ---------------------------------------------------------------------------
def get_visitor_session(session_id: str) -> dict | None:
    return _get_doc("visitor_sessions", "session_id", session_id)


def create_visitor_session(session_id: str, data: dict) -> None:
    data = dict(data)
    now = _now()
    data["first_seen"] = now
    data["last_seen"] = now
    data["pageview_count"] = 0
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO visitor_sessions(session_id, last_seen, data) VALUES(?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET last_seen=excluded.last_seen, "
            "data=excluded.data",
            (session_id, now, json.dumps(data)),
        )
        conn.commit()


def add_visitor_pageview(session_id: str, path: str, title: str) -> None:
    now = _now()
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO visitor_pageviews(session_id, path, title, created_at) VALUES(?,?,?,?)",
            (session_id, path, title, now),
        )
        row = conn.execute(
            "SELECT data FROM visitor_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row:
            data = json.loads(row["data"])
            data["pageview_count"] = data.get("pageview_count", 0) + 1
            data["last_seen"] = now
            conn.execute(
                "UPDATE visitor_sessions SET last_seen=?, data=? WHERE session_id=?",
                (now, json.dumps(data), session_id),
            )
        conn.commit()


def list_visitor_sessions(limit: int = 500) -> list[dict]:
    out = []
    for row in _connect().execute(
        "SELECT session_id, data FROM visitor_sessions ORDER BY last_seen DESC LIMIT ?",
        (limit,),
    ):
        data = json.loads(row["data"])
        data["session_id"] = row["session_id"]
        out.append(data)
    return out


def reset_stuck_jobs() -> None:
    """Crash recovery: anything left 'processing' goes back to pending."""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT uid, job_id, data FROM jobs WHERE status='processing'"
        ).fetchall()
        for row in rows:
            data = json.loads(row["data"])
            data["status"] = "pending"
            data["updated_at"] = _now()
            conn.execute(
                "UPDATE jobs SET status='pending', data=? WHERE uid=? AND job_id=?",
                (json.dumps(data), row["uid"], row["job_id"]),
            )
        if rows:
            logger.info(f"reset {len(rows)} stuck job(s) to pending")
        conn.commit()
