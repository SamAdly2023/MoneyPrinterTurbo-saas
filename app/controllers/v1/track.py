"""
Public pageview beacon for the marketing site's visitor tracker (see the
inline <script> on landing/login/privacy/terms in resource/public). No auth -
anonymous visitors are exactly who this records, so this path must stay
listed in asgi.py's _PUBLIC_PATHS or auth_gate will 401 every request.
"""

from typing import Optional

from fastapi import Request
from loguru import logger
from pydantic import BaseModel

from app.controllers.v1.base import new_router
from app.services import auth, visitors
from app.utils import utils

router = new_router()


class PageviewBody(BaseModel):
    session_id: str
    path: str
    title: Optional[str] = ""
    referrer: Optional[str] = ""
    utm_source: Optional[str] = ""
    utm_medium: Optional[str] = ""
    utm_campaign: Optional[str] = ""
    utm_term: Optional[str] = ""
    utm_content: Optional[str] = ""


def _client_ip(request: Request) -> str:
    # Cloud Run sits behind a load balancer - request.client.host is the
    # LB's internal address, not the visitor's; the real IP is the first
    # hop in X-Forwarded-For.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.post("/track/pageview", summary="Record one anonymous pageview on a public marketing page")
def track_pageview(request: Request, body: PageviewBody):
    # Don't track the admin's own visits to their own marketing pages.
    user = auth.get_current_user(request)
    if user and user.get("is_admin"):
        return utils.get_response(200, {"tracked": False})
    if not body.session_id or not body.path:
        return utils.get_response(200, {"tracked": False})
    try:
        visitors.record_pageview(
            session_id=body.session_id[:100],
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
            path=body.path[:300],
            title=(body.title or "")[:200],
            referrer=(body.referrer or "")[:500],
            utm={
                "utm_source": body.utm_source or "",
                "utm_medium": body.utm_medium or "",
                "utm_campaign": body.utm_campaign or "",
                "utm_term": body.utm_term or "",
                "utm_content": body.utm_content or "",
            },
        )
    except Exception as e:  # noqa: BLE001 - tracking must never break the page for a visitor
        logger.warning(f"pageview tracking failed: {e}")
        return utils.get_response(200, {"tracked": False})
    return utils.get_response(200, {"tracked": True})
