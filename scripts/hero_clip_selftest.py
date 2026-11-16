"""Make a few sample AI hero clips (Wan 2.2) so you can judge quality and price before it goes in the product.

    cd ~/moneyprinterturbo
    ~/virtualenv/moneyprinterturbo/3.11/bin/python scripts/hero_clip_selftest.py
    ~/virtualenv/moneyprinterturbo/3.11/bin/python scripts/hero_clip_selftest.py --count 3 --resolution 720p
    ~/virtualenv/moneyprinterturbo/3.11/bin/python scripts/hero_clip_selftest.py --cleanup

It uses the Replicate token already saved in the admin Settings (the same one the AI avatar uses) and the
AI text service to write each clip's prompt. It does NOT touch jobs, credits or any user's videos. Each
clip costs a few cents on your Replicate account. The clips are saved under the site's /media/ folder
with hard-to-guess names so you can open them in a browser; run with --cleanup afterwards to delete them.

Options:
    --count N            how many topics to try (default 4, max 8)
    --resolution 480p|720p   (default 480p)
    --aspect 9:16|16:9       (default 9:16)
    --no-smooth          keep the model's native 16 fps instead of smoothing to 30 fps
    --email x@y.com      whose AI text-service settings to use (default: newest user)
    --cleanup            delete earlier sample clips and exit
"""

import argparse
import glob
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.environ.setdefault("MPT_CONFIG_FILE", os.path.join(BASE_DIR, "config.toml"))


def _load_env_file(path: str) -> None:
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

from app.services import firestore_db, hero_clip, saas  # noqa: E402
from app.services.publish import base_url  # noqa: E402
from app.utils import utils  # noqa: E402

PREFIX = "_herotest-"

# (video topic, first line of the narration) - typical Vidzy subjects.
TOPICS = [
    ("roof maintenance tips", "Small roof leaks cost a fortune when they are ignored."),
    ("how to save money on groceries", "Your grocery bill is quietly eating your paycheck."),
    ("morning workout routine", "Ten minutes every morning can change your whole day."),
    ("best coffee brewing methods", "Most people make their coffee wrong."),
    ("tips for a first-time home buyer", "Buying your first home is the biggest decision of your life."),
    ("the history of the Roman Empire", "At its peak, Rome ruled over a quarter of the world."),
    ("how solar panels work", "Every second, the sun sends more energy to Earth than we could ever use."),
    ("running a small restaurant", "Behind every busy kitchen is a chef who never stops moving."),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=4)
    ap.add_argument("--resolution", default="480p", choices=["480p", "720p"])
    ap.add_argument("--aspect", default="9:16", choices=["9:16", "16:9"])
    ap.add_argument("--no-smooth", action="store_true")
    ap.add_argument("--email", default="")
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()

    out_dir = utils.storage_dir("output", create=True)
    if args.cleanup:
        old = glob.glob(os.path.join(out_dir, PREFIX + "*"))
        for p in old:
            os.remove(p)
        print(f"deleted {len(old)} sample clip(s)")
        return 0

    users = sorted(firestore_db.list_users(), key=lambda u: u.get("created_at", ""), reverse=True)
    if args.email:
        users = [u for u in users if args.email.lower() in (u.get("email") or "").lower()]
    if not users:
        print("RESULT: FAILED: no matching user to borrow the AI text settings from")
        return 2
    uid = users[0]["uid"]
    if not (firestore_db.get_global_settings().get("replicate_api_token") or "").strip():
        print("RESULT: FAILED: no Replicate token saved in the admin Settings (the AI avatar field).")
        return 2

    batch = utils.get_uuid(remove_hyphen=True)[:8]
    ok = 0
    total = min(max(args.count, 1), len(TOPICS))
    print(f"making {total} clip(s) at {args.resolution}, {args.aspect} - about 1-3 minutes each\n")
    for i, (subject, opening) in enumerate(TOPICS[:total], 1):
        with saas._user_config_scope(uid):
            prompt = hero_clip.build_prompt(subject, opening)
        print(f"[{i}/{total}] topic : {subject}\n        prompt: {prompt}")
        name = f"{PREFIX}{batch}-{i}.mp4"
        started = time.time()
        try:
            res = hero_clip.generate_hero_clip(
                prompt, os.path.join(out_dir, name), aspect=args.aspect,
                resolution=args.resolution, smooth_to_30fps=not args.no_smooth,
            )
        except Exception as e:  # noqa: BLE001
            print(f"        FAILED after {time.time() - started:.0f}s: {type(e).__name__}: {e}\n")
            continue
        ok += 1
        size = os.path.getsize(res["path"]) / (1024 * 1024)
        print(f"        OK in {res['seconds']:.0f}s, {size:.1f} MB, prediction {res['id']}")
        print(f"        watch : {base_url()}/media/{name}\n")

    print(f"RESULT: {ok}/{total} clips made.")
    if ok:
        print("Open the links above to judge them. Check the real cost under Replicate > Billing "
              "(about $0.05 per 480p clip is expected). When done, run again with --cleanup.")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
