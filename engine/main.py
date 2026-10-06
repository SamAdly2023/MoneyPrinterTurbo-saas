"""Vidzy Engine entry point.

    python -m engine --server https://vidzone.live --key vde_xxxxx

The key comes from the Vidzy dashboard (API tab -> Vidzy Engine). It is saved in
the Engine's data folder, so afterwards plain `python -m engine` is enough.
"""

import argparse
import json
import os
import sys
import time

from engine import __version__
from engine.client import EngineClient, JobLost, KeyRejected

DEFAULT_SERVER = "https://vidzone.live"


def default_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "VidzyEngine")


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


class Engine:
    def __init__(self, client, renderer_factory, sleep=time.sleep, log_fn=log):
        self.client = client
        self.renderer_factory = renderer_factory
        self.sleep = sleep
        self.log = log_fn
        self._renderer = None
        self.running = True

    # -- one job -------------------------------------------------------------
    def process(self, job: dict) -> None:
        job_id, nonce = job["id"], job["claim_nonce"]
        self.log(f"Rendering: {job.get('title') or job_id}")
        state = {"last_pct": -10, "last_at": 0.0, "lost": False}

        def on_progress(pct: int) -> None:
            now = time.time()
            if state["lost"] or pct - state["last_pct"] < 2 or now - state["last_at"] < 5:
                return
            state["last_pct"], state["last_at"] = pct, now
            try:
                self.client.progress(job_id, nonce, pct)
            except JobLost:
                state["lost"] = True
            except Exception:  # noqa: BLE001 - a missed progress ping is harmless
                pass

        video_path = None
        try:
            if self._renderer is None:
                self._renderer = self.renderer_factory(on_progress)
            self._renderer.on_progress = on_progress
            video_path, script = self._renderer.render(job)
            if state["lost"]:
                self.log("The server gave this job to its own renderer - skipping upload.")
                return
            self.log("Uploading the finished video...")
            self.client.complete(job_id, nonce, video_path, script)
            self.log(f"Done: {job.get('title') or job_id}")
        except JobLost:
            self.log("The server gave this job to its own renderer - skipping upload.")
        except KeyRejected:
            raise
        except Exception as e:  # noqa: BLE001
            self.log(f"Render failed: {e}")
            try:
                self.client.fail(job_id, nonce, str(e))
            except KeyRejected:
                raise
            except Exception:  # noqa: BLE001
                pass  # the server's stall check will hand the job back
        finally:
            if video_path and self._renderer:
                self._renderer.cleanup(video_path)

    # -- main loop -----------------------------------------------------------
    def run(self) -> int:
        backoff = 10
        announced = None
        while self.running:
            try:
                hb = self.client.heartbeat()
                backoff = 10
                if announced != hb.get("email"):
                    announced = hb.get("email")
                    self.log(f"Connected as {announced}. Waiting for videos to render...")
                if not hb.get("enabled"):
                    self.sleep(hb.get("poll_seconds", 30))
                    continue
                if hb.get("pending", 0) > 0:
                    job = self.client.claim()
                    if job:
                        self.process(job)
                        continue  # check for more work straight away
                self.sleep(hb.get("poll_seconds", 30))
            except KeyRejected as e:
                self.log(f"Stopped: {e}")
                return 2
            except Exception as e:  # noqa: BLE001 - server/network trouble: back off and retry
                self.log(f"Can't reach the server ({e}). Retrying in {backoff}s...")
                self.sleep(backoff)
                backoff = min(backoff * 2, 300)
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vidzy-engine", description="Render Vidzy videos on this computer.")
    ap.add_argument("--server", help=f"Vidzy server (default {DEFAULT_SERVER})")
    ap.add_argument("--key", help="Engine key from the Vidzy dashboard")
    ap.add_argument("--data-dir", default=default_data_dir(), help="where the Engine keeps its settings and files")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args(argv)
    if args.version:
        print(__version__)
        return 0

    os.makedirs(args.data_dir, exist_ok=True)
    cfg_path = os.path.join(args.data_dir, "engine.json")
    cfg = {}
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        pass
    server = args.server or cfg.get("server") or DEFAULT_SERVER
    key = args.key or cfg.get("key") or os.environ.get("VIDZY_ENGINE_KEY", "")
    if not key:
        log("No Engine key yet. Get one in the Vidzy dashboard (API tab -> Vidzy Engine), then run:")
        log("    vidzy-engine --key vde_xxxxxxxx")
        return 1
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"server": server, "key": key}, fh)

    from engine.renderer import Renderer, setup_environment

    setup_environment(args.data_dir)
    log(f"Vidzy Engine {__version__} - {server}")
    return Engine(EngineClient(server, key), Renderer).run()


if __name__ == "__main__":
    sys.exit(main())
