"""MySQL data layer - for running the app on cPanel/shared hosting.

Same 34+ functions as db_firestore.py and db_sqlite.py, same shapes in and
out, so nothing above this layer knows which one it is talking to (see
firestore_db.py, the facade that picks between them via MPT_DB).

Storage model matches db_sqlite.py exactly: each row keeps the whole document
under a `data` JSON column, with only the fields the queue actually queries
(status, created_at) promoted to real columns. Ported here rather than
reinvented, because it was already proven correct against a real database.

Two things differ from the SQLite version because MySQL actually is a
different database, not just a different driver:

- Locking. SQLite's `BEGIN IMMEDIATE` grabs a whole-database write lock up
  front - fine for a single local file, wasteful and wrong for a real
  multi-connection MySQL server. The MySQL equivalent is `SELECT ... FOR
  UPDATE` inside a transaction, which locks only the row(s) actually being
  claimed. Every function that does claim-and-mark (jobs, auto-mode users,
  invites, the YouTube quota counter) uses that pattern instead.

- Reconnection. Shared hosting MySQL commonly has a short `wait_timeout`
  (closes idle connections after a few minutes), which a long-lived SQLite
  file connection never has to worry about. `_connect()` pings and
  transparently reconnects before every use.

Configure via env vars (matching a typical cPanel "MySQL Databases" page):
  MPT_MYSQL_HOST      default "localhost"
  MPT_MYSQL_PORT      default 3306
  MPT_MYSQL_DB        required
  MPT_MYSQL_USER      required
  MPT_MYSQL_PASSWORD  required
"""

import json
import os
import threading
import datetime

import pymysql
import pymysql.cursors
from loguru import logger

from app.config import config

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

# Same seed list as db_sqlite.py: on first run only, so a fresh MySQL
# database starts with working API keys instead of a blank Settings page.
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


def _connect():
    global _conn
    if _conn is not None:
        try:
            _conn.ping(reconnect=True)
            return _conn
        except Exception:  # noqa: BLE001 - fall through and open a fresh one
            _conn = None

    _conn = pymysql.connect(
        host=os.getenv("MPT_MYSQL_HOST", "localhost"),
        port=int(os.getenv("MPT_MYSQL_PORT", "3306")),
        db=os.getenv("MPT_MYSQL_DB", ""),
        user=os.getenv("MPT_MYSQL_USER", ""),
        password=os.getenv("MPT_MYSQL_PASSWORD", ""),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    _init_schema(_conn)
    logger.success(
        "MySQL database: {}@{}:{}/{}".format(
            os.getenv("MPT_MYSQL_USER", ""),
            os.getenv("MPT_MYSQL_HOST", "localhost"),
            os.getenv("MPT_MYSQL_PORT", "3306"),
            os.getenv("MPT_MYSQL_DB", ""),
        )
    )
    return _conn


def _init_schema(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                uid  VARCHAR(191) PRIMARY KEY,
                data LONGTEXT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                uid        VARCHAR(191) NOT NULL,
                job_id     VARCHAR(191) NOT NULL,
                status     VARCHAR(32),
                created_at VARCHAR(64),
                data       LONGTEXT NOT NULL,
                PRIMARY KEY (uid, job_id),
                INDEX idx_jobs_status (status, created_at),
                INDEX idx_jobs_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_config (
                `key` VARCHAR(64) PRIMARY KEY,
                data  LONGTEXT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS invites (
                token       VARCHAR(64) PRIMARY KEY,
                email       VARCHAR(255),
                created_by  VARCHAR(191),
                created_at  VARCHAR(64),
                used_at     VARCHAR(64) NULL,
                used_by_uid VARCHAR(191) NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS visitor_sessions (
                session_id VARCHAR(191) PRIMARY KEY,
                last_seen  VARCHAR(64),
                data       LONGTEXT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS visitor_pageviews (
                id         BIGINT AUTO_INCREMENT PRIMARY KEY,
                session_id VARCHAR(191),
                path       VARCHAR(512),
                title      VARCHAR(255),
                created_at VARCHAR(64)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_payments (
                payment_id VARCHAR(191) PRIMARY KEY,
                uid        VARCHAR(191),
                credits    INT,
                created_at VARCHAR(64)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
    conn.commit()
    _seed_global_settings(conn)


def _seed_global_settings(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM app_config WHERE `key`='global'")
        if cur.fetchone() is not None:
            return
        seed = dict(DEFAULT_GLOBAL_SETTINGS)
        for key in _SEED_FROM_CONFIG:
            value = config.app.get(key)
            if value not in (None, "", [], {}):
                seed[key] = value
        cur.execute(
            "INSERT INTO app_config(`key`, data) VALUES('global', %s)",
            (json.dumps(seed),),
        )
    conn.commit()
    logger.info(f"seeded MySQL settings from config.toml ({len(seed)} keys)")


def _get_doc(table: str, key_col: str, key: str) -> dict | None:
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute(f"SELECT data FROM {table} WHERE `{key_col}`=%s", (key,))
        row = cur.fetchone()
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
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM users WHERE uid=%s", (uid,))
            row = cur.fetchone()
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
            cur.execute(
                "INSERT INTO users(uid, data) VALUES(%s, %s)", (uid, json.dumps(doc))
            )
        conn.commit()
        doc["uid"] = uid
        return doc, True


def list_users() -> list[dict]:
    conn = _connect()
    out = []
    with conn.cursor() as cur:
        cur.execute("SELECT uid, data FROM users")
        for row in cur.fetchall():
            data = json.loads(row["data"])
            data["uid"] = row["uid"]
            out.append(data)
    return out


def _update_user(uid: str, changes: dict, delete_keys: tuple = ()) -> None:
    with _lock:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM users WHERE uid=%s", (uid,))
            row = cur.fetchone()
            if not row:
                return
            data = json.loads(row["data"])
            data.update(changes)
            for k in delete_keys:
                data.pop(k, None)
            cur.execute(
                "UPDATE users SET data=%s WHERE uid=%s", (json.dumps(data), uid)
            )
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
    mid-batch. SELECT ... FOR UPDATE locks the row for the transaction,
    matching reserve_youtube_upload_slot's pattern (a per-user row here, a
    shared counter row there)."""
    with _lock:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM users WHERE uid=%s FOR UPDATE", (uid,))
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return False
                data = json.loads(row["data"])
                balance = int(data.get("credits", 0))
                if balance < count:
                    conn.rollback()
                    return False
                data["credits"] = balance - count
                cur.execute(
                    "UPDATE users SET data=%s WHERE uid=%s", (json.dumps(data), uid)
                )
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
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM users WHERE uid=%s", (uid,))
            row = cur.fetchone()
            if not row:
                return
            data = json.loads(row["data"])
            data["credits"] = int(data.get("credits", 0)) + count
            cur.execute("UPDATE users SET data=%s WHERE uid=%s", (json.dumps(data), uid))
        conn.commit()


def add_credits(uid: str, count: int) -> None:
    """Plain grant - admin top-ups. The PayPal purchase path uses
    record_payment_if_new instead, which wraps this with idempotency."""
    with _lock:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM users WHERE uid=%s", (uid,))
            row = cur.fetchone()
            if not row:
                return
            data = json.loads(row["data"])
            data["credits"] = int(data.get("credits", 0)) + count
            cur.execute("UPDATE users SET data=%s WHERE uid=%s", (json.dumps(data), uid))
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
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM processed_payments WHERE payment_id=%s FOR UPDATE",
                    (payment_id,),
                )
                if cur.fetchone():
                    conn.rollback()
                    return False
                try:
                    cur.execute(
                        "INSERT INTO processed_payments(payment_id, uid, credits, created_at) "
                        "VALUES(%s,%s,%s,%s)",
                        (payment_id, uid, count, _now()),
                    )
                except pymysql.err.IntegrityError:
                    # Lost a tight race to another request for the same payment_id -
                    # the PRIMARY KEY constraint is the real backstop the FOR UPDATE
                    # check above only narrows; treat this exactly like "already
                    # processed" rather than surfacing a raw DB error.
                    conn.rollback()
                    return False
                cur.execute("SELECT data FROM users WHERE uid=%s FOR UPDATE", (uid,))
                urow = cur.fetchone()
                if not urow:
                    conn.rollback()
                    return False
                data = json.loads(urow["data"])
                data["credits"] = int(data.get("credits", 0)) + count
                cur.execute(
                    "UPDATE users SET data=%s WHERE uid=%s", (json.dumps(data), uid)
                )
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
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM users WHERE uid=%s", (uid,))
            row = cur.fetchone()
            if not row:
                return
            data = json.loads(row["data"])
            merged = dict(data.get("profile") or {})
            merged.update(profile)
            data["profile"] = merged
            cur.execute(
                "UPDATE users SET data=%s WHERE uid=%s", (json.dumps(data), uid)
            )
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
        with conn.cursor() as cur:
            current = _get_doc("app_config", "key", "global") or {}
            current.update(settings)
            cur.execute(
                "INSERT INTO app_config(`key`, data) VALUES('global', %s) "
                "ON DUPLICATE KEY UPDATE data=VALUES(data)",
                (json.dumps(current),),
            )
        conn.commit()


def get_user_social(uid: str) -> dict:
    user = get_user(uid)
    return (user or {}).get("social", {}) or {}


def save_user_social(uid: str, platform: str, token_data: dict) -> None:
    with _lock:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM users WHERE uid=%s", (uid,))
            row = cur.fetchone()
            if not row:
                return
            data = json.loads(row["data"])
            social = dict(data.get("social") or {})
            merged = dict(social.get(platform) or {})
            merged.update(token_data)
            social[platform] = merged
            data["social"] = social
            cur.execute(
                "UPDATE users SET data=%s WHERE uid=%s", (json.dumps(data), uid)
            )
        conn.commit()


def clear_user_social(uid: str, platform: str) -> None:
    with _lock:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM users WHERE uid=%s", (uid,))
            row = cur.fetchone()
            if not row:
                return
            data = json.loads(row["data"])
            social = dict(data.get("social") or {})
            social.pop(platform, None)
            data["social"] = social
            cur.execute(
                "UPDATE users SET data=%s WHERE uid=%s", (json.dumps(data), uid)
            )
        conn.commit()


def claim_next_auto_mode_user(worker_id: str) -> dict | None:
    """Pick and mark the eligible user who waited longest, in one transaction.

    SELECT ... FOR UPDATE locks every candidate row for the duration of the
    transaction, so two workers evaluating this at once cannot both pick the
    same user before either commit lands.
    """
    with _lock:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT uid, data FROM users FOR UPDATE")
                candidates = []
                for row in cur.fetchall():
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
                cur.execute(
                    "UPDATE users SET data=%s WHERE uid=%s", (json.dumps(stored), uid)
                )
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
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs(uid, job_id, status, created_at, data) "
                "VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "status=VALUES(status), created_at=VALUES(created_at), data=VALUES(data)",
                (uid, job["id"], job.get("status"), job["created_at"], json.dumps(job)),
            )
        conn.commit()
    return job


def get_job(uid: str, job_id: str) -> dict | None:
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM jobs WHERE uid=%s AND job_id=%s", (uid, job_id))
        row = cur.fetchone()
    if not row:
        return None
    data = json.loads(row["data"])
    data["uid"] = uid
    return data


def list_jobs(uid: str) -> list[dict]:
    conn = _connect()
    out = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data FROM jobs WHERE uid=%s ORDER BY created_at DESC", (uid,)
        )
        for row in cur.fetchall():
            data = json.loads(row["data"])
            data["uid"] = uid
            out.append(data)
    return out


def list_all_jobs() -> list[dict]:
    conn = _connect()
    out = []
    with conn.cursor() as cur:
        cur.execute("SELECT uid, data FROM jobs ORDER BY created_at DESC")
        for row in cur.fetchall():
            data = json.loads(row["data"])
            data["uid"] = row["uid"]
            out.append(data)
    return out


def update_job(uid: str, job_id: str, **changes) -> None:
    changes["updated_at"] = _now()
    with _lock:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM jobs WHERE uid=%s AND job_id=%s", (uid, job_id)
            )
            row = cur.fetchone()
            if not row:
                return
            data = json.loads(row["data"])
            data.update(changes)
            cur.execute(
                "UPDATE jobs SET status=%s, data=%s WHERE uid=%s AND job_id=%s",
                (data.get("status"), json.dumps(data), uid, job_id),
            )
        conn.commit()


def delete_job(uid: str, job_id: str) -> None:
    with _lock:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE uid=%s AND job_id=%s", (uid, job_id))
        conn.commit()


def claim_next_pending_job(worker_id: str) -> tuple[str, dict] | None:
    """Claim the oldest pending job. SELECT ... FOR UPDATE locks the chosen
    row for the transaction, so two render threads cannot claim the same job."""
    with _lock:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT uid, job_id, data FROM jobs WHERE status='pending' "
                    "ORDER BY created_at LIMIT 1 FOR UPDATE"
                )
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return None
                data = json.loads(row["data"])
                data["status"] = "processing"
                data["claimed_by"] = worker_id
                data["updated_at"] = _now()
                cur.execute(
                    "UPDATE jobs SET status='processing', data=%s WHERE uid=%s AND job_id=%s",
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
        with conn.cursor() as cur:
            current = _get_doc("app_config", "key", "engine_state") or {}
            current.update(changes)
            cur.execute(
                "INSERT INTO app_config(`key`, data) VALUES('engine_state', %s) "
                "ON DUPLICATE KEY UPDATE data=VALUES(data)",
                (json.dumps(current),),
            )
        conn.commit()


def reserve_youtube_upload_slot(daily_cap: int) -> bool:
    if daily_cap <= 0:
        return True
    today = _now()[:10]
    with _lock:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM app_config WHERE `key`='youtube_quota' FOR UPDATE"
                )
                row = cur.fetchone()
                data = json.loads(row["data"]) if row else {}
                count = data.get("count", 0) if data.get("date") == today else 0
                if count >= daily_cap:
                    conn.rollback()
                    return False
                cur.execute(
                    "INSERT INTO app_config(`key`, data) VALUES('youtube_quota', %s) "
                    "ON DUPLICATE KEY UPDATE data=VALUES(data)",
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
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT uid, data FROM jobs WHERE status='processing'")
            for row in cur.fetchall():
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
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO invites(token, email, created_by, created_at, used_at, used_by_uid) "
                "VALUES(%s,%s,%s,%s,NULL,NULL)",
                (doc["token"], doc["email"], doc["created_by"], doc["created_at"]),
            )
        conn.commit()
    return doc


def get_invite(token: str) -> dict | None:
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT token, email, created_by, created_at, used_at, used_by_uid "
            "FROM invites WHERE token=%s", (token,)
        )
        row = cur.fetchone()
    return dict(row) if row else None


def claim_invite(token: str, email: str, uid: str) -> bool:
    """Single-use, and only by the address it was issued to.

    Both checks and the write happen inside one FOR UPDATE transaction, so
    two simultaneous sign-ups with the same link cannot both succeed.
    """
    email = (email or "").strip().lower()
    with _lock:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT email, used_at FROM invites WHERE token=%s FOR UPDATE",
                    (token,),
                )
                row = cur.fetchone()
                if not row or row["used_at"] or row["email"] != email:
                    conn.rollback()
                    return False
                cur.execute(
                    "UPDATE invites SET used_at=%s, used_by_uid=%s WHERE token=%s",
                    (_now(), uid, token),
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def list_invites() -> list[dict]:
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT token, email, created_by, created_at, used_at, used_by_uid "
            "FROM invites ORDER BY created_at DESC"
        )
        return [dict(row) for row in cur.fetchall()]


def delete_invite(token: str) -> None:
    with _lock:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM invites WHERE token=%s", (token,))
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
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO visitor_sessions(session_id, last_seen, data) VALUES(%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE last_seen=VALUES(last_seen), data=VALUES(data)",
                (session_id, now, json.dumps(data)),
            )
        conn.commit()


def add_visitor_pageview(session_id: str, path: str, title: str) -> None:
    now = _now()
    with _lock:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO visitor_pageviews(session_id, path, title, created_at) "
                "VALUES(%s,%s,%s,%s)",
                (session_id, path, title, now),
            )
            cur.execute(
                "SELECT data FROM visitor_sessions WHERE session_id=%s", (session_id,)
            )
            row = cur.fetchone()
            if row:
                data = json.loads(row["data"])
                data["pageview_count"] = data.get("pageview_count", 0) + 1
                data["last_seen"] = now
                cur.execute(
                    "UPDATE visitor_sessions SET last_seen=%s, data=%s WHERE session_id=%s",
                    (now, json.dumps(data), session_id),
                )
        conn.commit()


def list_visitor_sessions(limit: int = 500) -> list[dict]:
    conn = _connect()
    out = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT session_id, data FROM visitor_sessions ORDER BY last_seen DESC LIMIT %s",
            (limit,),
        )
        for row in cur.fetchall():
            data = json.loads(row["data"])
            data["session_id"] = row["session_id"]
            out.append(data)
    return out


def reset_stuck_jobs() -> None:
    """Crash recovery: anything left 'processing' goes back to pending."""
    with _lock:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT uid, job_id, data FROM jobs WHERE status='processing'")
            rows = cur.fetchall()
            for row in rows:
                data = json.loads(row["data"])
                data["status"] = "pending"
                data["updated_at"] = _now()
                cur.execute(
                    "UPDATE jobs SET status='pending', data=%s WHERE uid=%s AND job_id=%s",
                    (json.dumps(data), row["uid"], row["job_id"]),
                )
            if rows:
                logger.info(f"reset {len(rows)} stuck job(s) to pending")
        conn.commit()
