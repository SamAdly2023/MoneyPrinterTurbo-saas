"""
Firebase-backed session endpoints.

    POST /auth/session   exchange a verified Firebase ID token for our own
                          signed session cookie (creates the Firestore user
                          doc on first sign-in)
    GET  /auth/me         current session's {uid, email, is_admin}
    POST /auth/logout     clear the session cookie
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from app.controllers.v1.base import new_router
from app.services import auth, firestore_db
from app.utils import utils

router = new_router()


class SessionBody(BaseModel):
    id_token: str


@router.post("/auth/session", summary="Exchange a Firebase ID token for a session cookie")
def create_session(body: SessionBody):
    try:
        decoded = auth.verify_id_token(body.id_token)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ID token verification failed: {e}")
        return utils.get_response(401, message="invalid or expired sign-in token")

    uid, email, provider = decoded["uid"], decoded["email"], decoded["provider"]
    user = firestore_db.create_user_if_missing(uid, email, provider)
    if user.get("is_disabled"):
        return utils.get_response(403, message="this account has been disabled")

    response = JSONResponse(content=utils.get_response(200, {"uid": uid, "email": email}))
    response.set_cookie(
        key=auth.COOKIE_NAME,
        value=auth.create_auth_cookie_value(uid),
        max_age=auth.MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/auth/me", summary="Current session's user info")
def current_user(request: Request):
    user = request.state.user  # auth_gate has already verified this exists
    return utils.get_response(200, user)
