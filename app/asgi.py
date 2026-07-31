"""Application implementation - ASGI."""

import os

from fastapi import FastAPI, Form, Request
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

_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Sign in - MoneyPrinter Studio</title>
<style>
  :root{{
    --bg:#0b0f19; --panel:#141b2d; --border:#232d45; --text:#e7ecf5; --muted:#8b98b5;
    --accent:#6c5ce7; --grad:linear-gradient(135deg,#6c5ce7,#a06bff); --red:#ef4444;
  }}
  *{{box-sizing:border-box}}
  html,body{{margin:0;height:100%}}
  body{{
    background:radial-gradient(1200px 600px at 80% -10%,rgba(108,92,231,.18),transparent 60%),var(--bg);
    color:var(--text); font-family:'Segoe UI',system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif;
    display:flex;align-items:center;justify-content:center;
  }}
  .card{{
    width:340px;background:var(--panel);border:1px solid var(--border);border-radius:16px;
    padding:32px 28px;box-shadow:0 10px 30px rgba(0,0,0,.35);
  }}
  .logo{{width:44px;height:44px;border-radius:12px;background:var(--grad);display:grid;place-items:center;
    font-size:22px;margin-bottom:16px;}}
  h1{{font-size:18px;margin:0 0 4px}}
  p.sub{{margin:0 0 22px;color:var(--muted);font-size:13px}}
  label{{display:block;font-size:12.5px;color:var(--muted);margin:14px 0 6px;font-weight:600}}
  input{{
    width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--border);
    background:#0f1524;color:var(--text);font-size:14px;
  }}
  input:focus{{outline:none;border-color:var(--accent)}}
  button{{
    width:100%;margin-top:22px;padding:11px;border:none;border-radius:11px;
    background:var(--grad);color:#fff;font-weight:700;font-size:14px;cursor:pointer;
  }}
  button:hover{{filter:brightness(1.08)}}
  .error{{margin-top:14px;font-size:13px;color:var(--red)}}
</style>
</head>
<body>
  <form class="card" method="post" action="/login">
    <div class="logo">🎬</div>
    <h1>MoneyPrinter Studio</h1>
    <p class="sub">Sign in to continue</p>
    <label>Username</label>
    <input type="text" name="username" autocomplete="username" required autofocus />
    <label>Password</label>
    <input type="password" name="password" autocomplete="current-password" required />
    <button type="submit">Sign in</button>
    {error_html}
  </form>
</body>
</html>
"""

_PUBLIC_PATHS = {"/login", "/logout"}


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
    if path in _PUBLIC_PATHS:
        return await call_next(request)

    if auth.verify_auth_cookie(request.cookies.get(auth.COOKIE_NAME)):
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse(
            status_code=401,
            content=utils.get_response(401, None, "authentication required"),
        )

    return RedirectResponse(url="/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str = ""):
    error_html = (
        '<div class="error">Invalid username or password.</div>' if error else ""
    )
    return _LOGIN_PAGE.format(error_html=error_html)


@app.post("/login")
async def login_submit(username: str = Form(...), password: str = Form(...)):
    if not auth.verify_credentials(username, password):
        return RedirectResponse(url="/login?error=1", status_code=302)

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=auth.COOKIE_NAME,
        value=auth.create_auth_cookie_value(username),
        max_age=auth.MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


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
