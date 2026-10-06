"""HTTP client for the Vidzy server's Engine API (app/controllers/v1/engine.py)."""

import platform
import time

import requests

from engine import __version__

# Raised for conditions the main loop must react to rather than just retry.
class KeyRejected(Exception):
    """The server says this Engine key is invalid or was revoked."""


class JobLost(Exception):
    """The job was handed back to the server (the Engine was offline too long)."""


class EngineClient:
    def __init__(self, server: str, key: str, timeout: int = 60):
        self.server = server.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {key}",
            "User-Agent": f"VidzyEngine/{__version__}",
        })

    def _url(self, path: str) -> str:
        return f"{self.server}/api/v1/engine/v1{path}"

    def _call(self, method: str, path: str, **kw):
        r = self.session.request(method, self._url(path), timeout=kw.pop("timeout", self.timeout), **kw)
        try:
            body = r.json()
        except ValueError:
            raise RuntimeError(f"server returned a non-JSON reply (HTTP {r.status_code})")
        # The server reports errors in the JSON body, not always the HTTP status.
        code = body.get("status", r.status_code)
        if code == 401:
            raise KeyRejected(body.get("message", "Engine key rejected"))
        if code == 409:
            raise JobLost(body.get("message", "job is no longer assigned to this Engine"))
        if code >= 400:
            raise RuntimeError(f"{path}: {body.get('message', code)}")
        return body.get("data", body)

    def heartbeat(self) -> dict:
        return self._call("POST", "/heartbeat", json={
            "version": __version__,
            "platform": f"{platform.system()} {platform.release()}",
        })

    def claim(self):
        """The next job for this Engine, or None. The server may spend a while
        preparing it (script/search-term generation), hence the long timeout."""
        return self._call("POST", "/claim", timeout=180).get("job")

    def progress(self, job_id: str, nonce: str, pct: int) -> None:
        self._call("POST", f"/jobs/{job_id}/progress", json={"progress": int(pct), "claim_nonce": nonce})

    def fail(self, job_id: str, nonce: str, error: str) -> None:
        self._call("POST", f"/jobs/{job_id}/fail", json={"error": error[:500], "claim_nonce": nonce})

    def complete(self, job_id: str, nonce: str, video_path: str, script: str = "", attempts: int = 3) -> None:
        """Upload the finished MP4. Retried: a render is expensive to redo, a
        dropped connection on a big upload is not a reason to throw it away."""
        last = None
        for n in range(attempts):
            try:
                with open(video_path, "rb") as fh:
                    self._call(
                        "POST", f"/jobs/{job_id}/complete",
                        data={"claim_nonce": nonce, "script": script},
                        files={"file": (f"{job_id}.mp4", fh, "video/mp4")},
                        timeout=900,
                    )
                return
            except (KeyRejected, JobLost):
                raise
            except Exception as e:  # noqa: BLE001 - network hiccup or 5xx: retry
                last = e
                time.sleep(5 * (n + 1))
        raise RuntimeError(f"upload failed after {attempts} attempts: {last}")
