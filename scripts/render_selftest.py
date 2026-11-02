"""Run one complete test render on the server, without the dashboard.

    cd ~/moneyprinterturbo
    ~/virtualenv/moneyprinterturbo/3.11/bin/python scripts/render_selftest.py

It runs the real pipeline (voice-over -> subtitles -> stock footage -> final video) with the same
settings, API keys and library versions the website uses, prints each stage with the account's
process / thread / memory use, and ends with either `RESULT: OK` or `RESULT: FAILED: <reason>`.
It does not create a job, spend a credit or publish anything - the video is deleted afterwards
unless you pass --keep. It takes a few minutes; it needs the same limits as a real render, so it is
also a fair test of whether the host can finish one.

Options:
    --email someone@example.com   whose saved settings/profile to use (default: the newest user)
    --source pexels|pixabay       footage source (default: the platform's configured source)
    --keep                        keep the finished video and print where it is
"""

import argparse
import os
import shutil
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.environ.setdefault("MPT_CONFIG_FILE", os.path.join(BASE_DIR, "config.toml"))


def _load_env_file(path: str) -> None:
    """Same minimal KEY=VALUE loader the cron watchdog uses (database settings live in .env.cron)."""
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

from app.models import const  # noqa: E402
from app.models.schema import VideoParams  # noqa: E402
from app.services import firestore_db, saas  # noqa: E402
from app.services import state as sm  # noqa: E402
from app.services import task as tm  # noqa: E402
from app.utils import utils  # noqa: E402

SCRIPT = (
    "Small roof leaks cost a fortune when they are ignored. "
    "Check your flashing every spring, clear the gutters, and fix loose shingles early. "
    "A little maintenance now saves thousands later."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="")
    ap.add_argument("--source", default="")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    users = sorted(firestore_db.list_users(), key=lambda u: u.get("created_at", ""), reverse=True)
    if args.email:
        users = [u for u in users if args.email.lower() in (u.get("email") or "").lower()]
    if not users:
        print("RESULT: FAILED: no matching user to borrow settings from")
        return 2
    uid = users[0]["uid"]
    print(f"using the saved settings of {users[0].get('email')}")
    utils.log_resources("test start")

    task_id = "selftest-" + utils.get_uuid(remove_hyphen=True)[:10]
    started = time.time()
    result, error = None, ""
    try:
        with saas._user_config_scope(uid) as profile:
            params = saas.build_default_params(
                "roof maintenance tips", script=SCRIPT, terms="roof repair, house, rain, shingles",
                profile=profile, video_source=args.source, video_aspect="9:16",
            )
            result = tm.start(task_id=task_id, params=VideoParams(**params), stop_at="video")
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"

    task = sm.state.get_task(task_id) or {}
    videos = (result or {}).get("videos") or []
    took = time.time() - started
    utils.log_resources("test end")
    print()
    if videos and os.path.isfile(videos[0]):
        size = os.path.getsize(videos[0]) / (1024 * 1024)
        print(f"RESULT: OK - {size:.1f} MB video in {took:.0f}s")
        if args.keep:
            print(f"kept at: {videos[0]}")
        else:
            shutil.rmtree(os.path.dirname(videos[0]), ignore_errors=True)
        return 0
    reason = error or task.get("error") or "the pipeline stopped without saying why (see the log lines above)"
    print(f"RESULT: FAILED: {reason}   (after {took:.0f}s, state={task.get('state')}, progress={task.get('progress')})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
