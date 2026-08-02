"""
Admin-only endpoints for the professional admin dashboard.

Every handler here re-checks request.state.user["is_admin"] itself (in
addition to asgi.py's page-level /admin gate) since these are real data-
and control-plane actions, not just page views.
"""

from fastapi import Path, Request

from app.controllers.v1.base import new_router
from app.services import firestore_db, saas
from app.utils import utils

router = new_router()


def _require_admin(request: Request):
    user = request.state.user
    if not user.get("is_admin"):
        return utils.get_response(403, message="admin only")
    return None


@router.get("/admin/overview", summary="Admin dashboard overview stats")
def overview(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied

    users = firestore_db.list_users()
    jobs = saas.store.all_admin()
    counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    for j in jobs:
        counts[j.get("status", "")] = counts.get(j.get("status", ""), 0) + 1
    return utils.get_response(
        200,
        {
            "total_users": len(users),
            "disabled_users": sum(1 for u in users if u.get("is_disabled")),
            "auto_mode_users": sum(1 for u in users if u.get("settings", {}).get("auto_mode")),
            "total_videos": counts["done"],
            "job_counts": counts,
        },
    )


@router.get("/admin/users", summary="List every signed-up user")
def list_users(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied

    users = firestore_db.list_users()
    jobs = saas.store.all_admin()
    job_counts_by_uid = {}
    for j in jobs:
        job_counts_by_uid[j["uid"]] = job_counts_by_uid.get(j["uid"], 0) + 1
    for u in users:
        u["job_count"] = job_counts_by_uid.get(u["uid"], 0)
    users.sort(key=lambda u: u.get("created_at", ""), reverse=True)
    return utils.get_response(200, {"users": users})


@router.post("/admin/users/{uid}/disable", summary="Disable a user's account")
def disable_user(request: Request, uid: str = Path(...)):
    if (denied := _require_admin(request)) is not None:
        return denied
    if uid == request.state.user["uid"]:
        return utils.get_response(400, message="cannot disable your own admin account")
    firestore_db.set_user_disabled(uid, True)
    return utils.get_response(200, {"uid": uid, "is_disabled": True})


@router.post("/admin/users/{uid}/enable", summary="Re-enable a user's account")
def enable_user(request: Request, uid: str = Path(...)):
    if (denied := _require_admin(request)) is not None:
        return denied
    firestore_db.set_user_disabled(uid, False)
    return utils.get_response(200, {"uid": uid, "is_disabled": False})


@router.get("/admin/jobs", summary="Every job across every user")
def list_all_jobs(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    return utils.get_response(200, {"jobs": saas.store.all_admin()})


@router.get("/admin/engine", summary="Engine status")
def engine_status(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    return utils.get_response(200, saas.engine.status())


@router.post("/admin/engine/pause", summary="Pause the shared render queue for everyone")
def engine_pause(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    saas.engine.pause()
    return utils.get_response(200, saas.engine.status())


@router.post("/admin/engine/resume", summary="Resume the shared render queue")
def engine_resume(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    saas.engine.resume()
    return utils.get_response(200, saas.engine.status())


@router.post("/admin/engine/auto-kill/start", summary="Globally disable everyone's auto-mode")
def auto_kill_start(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    saas.engine.auto_kill_start()
    return utils.get_response(200, saas.engine.status())


@router.post("/admin/engine/auto-kill/stop", summary="Re-allow auto-mode generation")
def auto_kill_stop(request: Request):
    if (denied := _require_admin(request)) is not None:
        return denied
    saas.engine.auto_kill_stop()
    return utils.get_response(200, saas.engine.status())
