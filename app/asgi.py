"""Application implementation - ASGI."""

import os

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

_PUBLIC_PATHS = {
    "/", "/login", "/logout", "/api/v1/auth/session", "/logo.svg", "/logo.png", "/logo-icon.png",
    "/privacy", "/terms", "/robots.txt", "/sitemap.xml",
    # Anonymous visitor pageview beacon fired from the public marketing pages.
    "/api/v1/track/pageview",
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
    if path in _PUBLIC_PATHS or path.startswith("/media/") or path.startswith("/api/v1/external/"):
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


@app.get("/", response_class=HTMLResponse)
async def root_page(request: Request):
    # Signed-in visitors land on their dashboard; everyone else sees the
    # public marketing landing page instead of being bounced to /login.
    user = auth.get_current_user(request)
    return _read_public_html("index.html" if user else "landing.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _read_public_html("login.html")


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
def startup_event():
    logger.info("startup event")
    # Auto-start the video creation engine so saved scripts run one-by-one.
    from app.services import saas

    saas.engine.start()
