"""Kick off a Cloud Run Job execution instead of rendering in-process.

Rendering used to happen inside the web service, which is why that service
had to keep CPU allocated around the clock. Here the API only *starts* a
render - a Cloud Run Job execution that runs render_worker.py, drains the
queue and exits - so the service itself can be request-only and scale to zero.

Everything is opt-in through env vars, so nothing changes for local runs,
Docker users, or anyone self-hosting: with MPT_RENDER_MODE unset this module
reports "not enabled" and saas.create_job falls back to waking the in-process
engine exactly as before.

  MPT_RENDER_MODE=cloudrun_job   turn this on
  MPT_RENDER_JOB                 job name       (default moneyprinterturbo-render)
  MPT_RENDER_JOB_REGION          job region     (default us-central1)
  GOOGLE_CLOUD_PROJECT           project id     (already set on Cloud Run)
"""

import os
import threading

from loguru import logger

_MODE = os.getenv("MPT_RENDER_MODE", "inline").strip().lower()
_JOB_NAME = os.getenv("MPT_RENDER_JOB", "moneyprinterturbo-render").strip()
_REGION = os.getenv("MPT_RENDER_JOB_REGION", "us-central1").strip()

_session = None
_session_lock = threading.Lock()
_suppressed = False


def enabled() -> bool:
    return _MODE == "cloudrun_job"


def suppress(value: bool = True) -> None:
    """Stop trigger() from starting new executions in this process.

    The Auto Mode worker queues jobs by calling create_job, which would
    otherwise fire off a fresh Job execution per generated video. It renders
    them itself instead, so it suppresses dispatch first.
    """
    global _suppressed
    _suppressed = value


def _project() -> str:
    return (
        os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GCLOUD_PROJECT")
        or os.getenv("GCP_PROJECT")
        or ""
    ).strip()


def _authed_session():
    """One authorised session for the process - building it costs a metadata
    server round trip, and this runs on every queued job."""
    global _session
    if _session is not None:
        return _session
    with _session_lock:
        if _session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            _session = AuthorizedSession(creds)
    return _session


def trigger() -> bool:
    """Start one render execution. True if Cloud Run accepted it.

    Returns False - rather than raising - on any problem, so the caller can
    fall back to the in-process engine. A queued job that nobody renders is a
    much worse failure than one rendered the old way.
    """
    if not enabled():
        return False
    if _suppressed:
        # "Handled" - the caller must not fall back to the in-process engine
        # either, because the worker that suppressed dispatch renders it.
        return True

    project = _project()
    if not project:
        logger.warning("MPT_RENDER_MODE=cloudrun_job but no project id in env")
        return False

    url = (
        f"https://run.googleapis.com/v2/projects/{project}"
        f"/locations/{_REGION}/jobs/{_JOB_NAME}:run"
    )
    try:
        resp = _authed_session().post(url, json={}, timeout=30)
    except Exception as e:  # noqa: BLE001 - network/credential failures both fall back
        logger.error(f"could not reach Cloud Run to start a render job: {e}")
        return False

    if resp.status_code in (200, 201, 202):
        logger.info(f"started render job execution ({_JOB_NAME})")
        return True

    # 409 means an execution is already running. That is fine and expected:
    # the running worker drains the whole queue, so this job gets picked up.
    if resp.status_code == 409:
        logger.info("a render execution is already running - it will pick this up")
        return True

    logger.error(
        f"Cloud Run refused to start the render job ({resp.status_code}): {resp.text[:300]}"
    )
    return False
