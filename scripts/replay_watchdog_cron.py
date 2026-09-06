"""Standalone entry point for the cron-driven replay watchdog.

Runs app.services.replay's watchdog tick once and exits - meant to be
invoked by a cPanel cron job (every minute), not run directly.

Why this exists alongside the in-app watchdog thread (replay.start_watchdog(),
started from app/asgi.py): a subprocess inherits the systemd login-session
scope of whatever process spawned it. ffmpeg pushes started from inside the
Passenger app worker share that worker's session scope, so when cPanel/
CloudLinux recycles the worker, the whole scope - and every ffmpeg push it
started, including ones the in-app watchdog restarted - gets torn down with
it, regardless of subprocess.Popen(start_new_session=True). A cron job gets
its own independent session scope (confirmed empirically on this host: a
cron-launched, detached process was still alive well after the cron
invocation that started it had already exited), so an ffmpeg push restarted
from here survives the next Passenger recycle instead of dying with it.

The in-app thread is left running too, as a faster (30s vs. up to 60s)
first responder for deaths unrelated to a worker recycle - both call the
same is_alive()-gated restart, so at worst one of them occasionally finds
nothing to do.
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.environ.setdefault("MPT_CONFIG_FILE", os.path.join(BASE_DIR, "config.toml"))


def _load_env_file(path: str) -> None:
    """Cron jobs don't inherit cPanel's Setup-Python-App environment (that's
    injected by Passenger/CloudLinux only for requests it spawns), so the DB
    connection vars this script needs live in a plain KEY=VALUE file instead
    - deliberately not committed to git (see .gitignore) and chmod 600 on
    the server. Minimal parser, no need for python-dotenv for eight lines."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file(os.path.join(BASE_DIR, ".env.cron"))

from app.services import replay  # noqa: E402

if __name__ == "__main__":
    replay.run_watchdog_tick()
