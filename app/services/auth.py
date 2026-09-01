"""Multi-user Firebase-backed auth for the SaaS dashboard.

A verified Firebase ID token is exchanged once (POST /api/v1/auth/session)
for our own signed session cookie carrying just the uid. Authorization
facts (email, is_admin, is_disabled) are always re-derived live from
Firestore on every request rather than trusted from the cookie payload -
that's what lets an admin disable an abusive account and have it take
effect immediately instead of waiting out a 30-day cookie.

Keeping the cookie (rather than switching to a Bearer-token scheme) is
deliberate: plain <video>/<img> tags under /media and /tasks rely on the
browser sending it automatically, which a header-based scheme can't do.
"""

import secrets

from fastapi import Request
from firebase_admin import auth as firebase_auth
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import config
from app.services import billing
from app.services import firebase_init  # noqa: F401 - ensures app is initialized first
from app.services import firestore_db

ADMIN_EMAILS = {"samadly728@gmail.com"}

# Must be named exactly "__session" - Firebase Hosting's rewrite proxy to
# Cloud Run strips every other cookie name before forwarding the request.
COOKIE_NAME = "__session"
MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _get_session_secret() -> str:
    secret = config.app.get("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        config.app["session_secret"] = secret
        config.save_config()
    return secret


_serializer = URLSafeTimedSerializer(_get_session_secret())


def verify_id_token(id_token: str) -> dict:
    """Verify a Firebase ID token. Returns {uid, email, provider}. Raises on failure."""
    decoded = firebase_auth.verify_id_token(id_token)
    uid = decoded["uid"]
    email = (decoded.get("email") or "").lower()
    provider = decoded.get("firebase", {}).get("sign_in_provider", "unknown")
    return {"uid": uid, "email": email, "provider": provider}


def create_auth_cookie_value(uid: str) -> str:
    return _serializer.dumps({"uid": uid})


def _decode_cookie(value: str) -> str | None:
    if not value:
        return None
    try:
        payload = _serializer.loads(value, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return payload.get("uid")


def get_current_user(request: Request) -> dict | None:
    """Return {uid, email, is_admin} for the request's session, or None.

    Re-reads is_disabled/email from Firestore on every call by design (see
    module docstring) - a disabled account loses access immediately.
    """
    uid = _decode_cookie(request.cookies.get(COOKIE_NAME))
    if not uid:
        return None
    user = firestore_db.get_user(uid)
    if not user or user.get("is_disabled"):
        return None
    email = (user.get("email") or "").lower()
    is_owner = email in ADMIN_EMAILS
    return {
        "uid": uid,
        "email": email,
        "is_admin": is_owner or bool(user.get("is_admin")),
        "is_owner": is_owner,
        # Per-user capability switches, re-derived per request like is_admin
        # so an admin revoking them takes effect immediately rather than at
        # the user's next login. Absent means allowed, so accounts that
        # predate the feature keep working untouched.
        "can_render": bool(user.get("can_render", True)),
        "can_clip": bool(user.get("can_clip", True)),
        # Credits/subscription are meaningless on the local desktop app
        # (billing.live_billing_enabled() is False there), but harmless to
        # always include - the dashboard only shows the credits badge / Auto
        # Mode paywall prompt when the live-site flag is also true.
        "credits": int(user.get("credits", 0)),
        "auto_mode_subscription_active": bool(user.get("auto_mode_subscription_active")),
        "live_billing_enabled": billing.live_billing_enabled(),
    }
