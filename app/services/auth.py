"""Single-user login gate for the SaaS dashboard.

Hardcoded credentials on purpose: this app has no user database, it's meant
for one operator. The session secret is generated once and persisted to
config.toml so cookies survive restarts.
"""

import hmac
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import config

_ADMIN_USERNAMES = {"admin", "samadly728@gmail.com"}
_ADMIN_PASSWORD = "samadly728@gmail.com"

COOKIE_NAME = "mpt_auth"
MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _get_session_secret() -> str:
    secret = config.app.get("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        config.app["session_secret"] = secret
        config.save_config()
    return secret


_serializer = URLSafeTimedSerializer(_get_session_secret())


def verify_credentials(username: str, password: str) -> bool:
    username = (username or "").strip().lower()
    username_ok = any(
        hmac.compare_digest(username, u) for u in _ADMIN_USERNAMES
    )
    password_ok = hmac.compare_digest(password or "", _ADMIN_PASSWORD)
    return username_ok and password_ok


def create_auth_cookie_value(username: str) -> str:
    return _serializer.dumps({"user": username})


def verify_auth_cookie(value: str) -> bool:
    if not value:
        return False
    try:
        _serializer.loads(value, max_age=MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False
