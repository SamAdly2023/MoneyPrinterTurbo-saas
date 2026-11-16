"""Application implementation - ASGI."""

import os

# The shared host caps threads per account (NPROC), and every app process counts
# against it. numpy/OpenBLAS/OpenMP/ctranslate2 each start one thread PER CPU
# CORE by default - dozens per process on a big server - which is what ends in
# "pthread_create() failed: Resource temporarily unavailable" and ffmpeg dying
# mid-render. Pin them to one before anything imports them. setdefault, so a
# machine that wants more (the Engine, a dev PC) can still override via env.
for _var in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "CT2_INTER_THREADS",
):
    os.environ.setdefault(_var, "1")
# glibc gives every thread its own 64MB *virtual* malloc arena. On CloudLinux the
# account's virtual-memory cap counts those reservations, so a multi-threaded
# ffmpeg can fail "pthread_create(): Resource temporarily unavailable" with
# plenty of real memory free. Two arenas keeps it small; ffmpeg children inherit it.
os.environ.setdefault("MALLOC_ARENA_MAX", "2")

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import config
from app.models.exception import HttpException
from app.router import root_api_router
from app.services import auth
from app.utils import utils
from app.utils.security_headers import SecurityHeadersMiddleware

_PUBLIC_PATHS = {
    "/", "/login", "/logout", "/api/v1/auth/session", "/logo.svg", "/logo.png", "/logo-icon.png",
    "/privacy", "/terms", "/data-deletion", "/robots.txt", "/sitemap.xml", "/manifest.json",
    # Prices for the landing page's pricing section (read from the same admin settings the dashboard uses).
    "/api/v1/public/pricing",
    # Anonymous visitor pageview beacon fired from the public marketing pages.
    "/api/v1/track/pageview",
    # PayPal's server calling us directly - no browser session cookie exists.
    # Safe to leave unauthenticated because the handler verifies PayPal's
    # signature on every request before trusting the body (see
    # billing.verify_webhook_signature) - that check, not a cookie, is what
    # makes this endpoint trustworthy.
    "/api/v1/saas/paypal/webhook",
    # TikTok domain-ownership verification file (Developer Portal -> URL properties)
    "/tiktokxb2Qi9CpEIW2FcFuDD6btN9J9HKbErcD.txt",
}


def _read_public_html(name: str) -> str:
    with open(os.path.join(utils.public_dir(), name), encoding="utf-8") as f:
        return f.read()


def exception_handler(request: Request, e: HttpException):
    return JSONResponse(
        status_code=e.status_code,
        content=utils.get_response(e.status_code, e.data, e.message),
    )


def validation_exception_handler(request: Request, e: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content=utils.get_response(
            status=400, data=e.errors(), message="field required"
        ),
    )


def get_application() -> FastAPI:
    """Initialize FastAPI application.

    Returns:
       FastAPI: Application object instance.

    """
    instance = FastAPI(
        title=config.project_name,
        description=config.project_description,
        version=config.project_version,
        debug=False,
    )
    instance.include_router(root_api_router)
    instance.add_exception_handler(HttpException, exception_handler)
    instance.add_exception_handler(RequestValidationError, validation_exception_handler)
    return instance


app = get_application()

# Configures the CORS middleware for the FastAPI app
cors_allowed_origins_str = os.getenv("CORS_ALLOWED_ORIGINS", "")
origins = cors_allowed_origins_str.split(",") if cors_allowed_origins_str else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path

    # Every response this middleware touches is auth-dependent (even a
    # "public" path's response can vary by login state), so no CDN in front
    # of this app (e.g. the Firebase Hosting rewrite) may ever cache it -
    # doing so previously served a stale unauthenticated 302 back to a user
    # who had just logged in, because the edge cached the redirect and
    # replayed it regardless of the fresh, valid session cookie.
    def _no_store(response):
        response.headers["Cache-Control"] = "no-store, private"
        return response

    # /media/* (finished video files) must be fetchable without a session
    # cookie - Instagram's publish API fetches the video server-to-server by
    # URL, with no way to send our auth cookie. Filenames are unguessable
    # per-job UUIDs, the same trust model already relied on once a video is
    # actually published to YouTube/TikTok.
    # /api/v1/external/* authenticates itself via a per-user API key (see
    # controllers/v1/external.py) instead of the session cookie - a
    # different trust boundary entirely, meant to be called by other
    # platforms that have no way to hold a browser session.
    # /api/v1/engine/* is the downloadable Engine app, which authenticates with
    # its own Bearer key (controllers/v1/engine.py) - it has no browser session.
    if (
        path in _PUBLIC_PATHS
        or path.startswith("/media/")
        or path.startswith("/api/v1/external/")
        or path.startswith("/api/v1/engine/")
    ):
        return _no_store(await call_next(request))

    user = auth.get_current_user(request)
    if user is None:
        if path.startswith("/api/"):
            return _no_store(JSONResponse(
                status_code=401,
                content=utils.get_response(401, None, "authentication required"),
            ))
        return _no_store(RedirectResponse(url="/login", status_code=302))

    if path == "/admin" and not user["is_admin"]:
        return _no_store(JSONResponse(status_code=403, content=utils.get_response(403, None, "admin only")))

    request.state.user = user
    return _no_store(await call_next(request))


# Added after auth_gate on purpose: the last middleware added is the outermost, so these headers
# also land on auth_gate's own redirects and 401s.
app.add_middleware(SecurityHeadersMiddleware)


@app.get("/", response_class=HTMLResponse)
async def root_page(request: Request):
    # Signed-in visitors land on their dashboard; everyone else sees the
    # public marketing landing page instead of being bounced to /login.
    user = auth.get_current_user(request)
    return _read_public_html("index.html" if user else "landing.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _read_public_html("login.html")


_pricing_cache = {"at": 0.0, "data": None}


@app.get("/api/v1/public/pricing")
async def public_pricing():
    """What the landing page's pricing section shows. Everything comes from the admin settings, so the
    public page can never disagree with what checkout charges. Cached for a minute (public, no auth)."""
    import time

    if _pricing_cache["data"] is not None and time.time() - _pricing_cache["at"] < 60:
        return utils.get_response(200, _pricing_cache["data"])
    data = {"live": False, "streams": [], "video_plans": [], "credit_price_usd": 0}
    try:
        from app.services import billing, firestore_db, hero_clip, replay

        if billing.live_billing_enabled():
            data["live"] = True
            data["credit_price_usd"] = float(firestore_db.get_global_settings().get("credit_price_usd") or 0.75)
            for tier_id, tier in replay.effective_streams_plans().items():
                price = 0.0 if tier_id == "free" else billing.streams_plan_price(tier_id)
                if tier_id != "free" and price <= 0:
                    continue
                data["streams"].append({
                    "id": tier_id, "label": tier["label"], "max_concurrent": tier["max_concurrent"],
                    "max_duration_hours": tier["max_duration_hours"], "price_usd": price,
                })
            for tier_id, plan in billing.VIDEO_PLANS.items():
                if billing.video_plan_available(tier_id):
                    data["video_plans"].append({
                        "id": tier_id, "label": plan["label"], "credits": billing.video_plan_credits(tier_id),
                        "price_usd": billing.video_plan_price(tier_id),
                    })
            data["hero_clip"] = {"enabled": hero_clip.is_enabled(), "credits": hero_clip.credit_cost()}
    except Exception as e:  # noqa: BLE001 - the landing page must still render without prices
        logger.warning(f"public pricing unavailable: {e}")
        return utils.get_response(200, {"live": False, "streams": [], "video_plans": [], "credit_price_usd": 0})
    _pricing_cache.update(at=time.time(), data=data)
    return utils.get_response(200, data)


@app.get("/data-deletion", response_class=HTMLResponse)
async def data_deletion_page():
    return _read_public_html("data-deletion.html")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    return _read_public_html("privacy.html")


@app.get("/terms", response_class=HTMLResponse)
async def terms_page():
    return _read_public_html("terms.html")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return _read_public_html("admin.html")


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(auth.COOKIE_NAME)
    return response


task_dir = utils.task_dir()
app.mount(
    "/tasks", StaticFiles(directory=task_dir, html=True, follow_symlink=True), name=""
)

# Finished videos collected by the SaaS engine are served here.
media_dir = utils.storage_dir("output", create=True)
app.mount("/media", StaticFiles(directory=media_dir, html=False), name="media")

public_dir = utils.public_dir()
app.mount("/", StaticFiles(directory=public_dir, html=True), name="")


@app.on_event("shutdown")
def shutdown_event():
    logger.info("shutdown event")


@app.on_event("startup")
async def limit_request_threadpool():
    """FastAPI runs every sync endpoint in a thread pool that defaults to 40
    threads per process. Dashboard polling alone can fill it, and each one is a
    thread the host counts against the account."""
    try:
        import anyio.to_thread

        limit = int(os.getenv("MPT_REQUEST_THREADS", "10"))
        anyio.to_thread.current_default_thread_limiter().total_tokens = limit
        logger.info(f"request thread pool limited to {limit}")
    except Exception as e:  # noqa: BLE001 - an optimisation, never a reason to fail startup
        logger.warning(f"could not limit the request thread pool: {e}")


@app.on_event("startup")
def startup_event():
    logger.info("startup event")
    # Auto-start the video creation engine so saved scripts run one-by-one.
    from app.services import replay, saas

    saas.engine.start()
    # Auto-restarts real replay channels whose ffmpeg push died on its own
    # (crash, network drop, this very app process being recycled) so a
    # stream keeps running until the user clicks Stop.
    replay.start_watchdog()
