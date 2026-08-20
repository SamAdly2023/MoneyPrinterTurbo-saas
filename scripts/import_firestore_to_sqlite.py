"""One-time copy of Firestore data into the local SQLite database.

The local app starts empty, which is the point - it no longer shares the
hosted site's database. But two things are genuinely worth carrying across
rather than re-entering:

  settings  the shared OAuth app credentials (YouTube client id/secret,
            TikTok, Meta) and API keys. Without these the local app shows
            "YouTube client ID is not set".
  users     each account's profile and, more usefully, its *social tokens* -
            so a YouTube channel already connected on the hosted site is
            connected locally too, with no second OAuth round trip. Refresh
            tokens do not care which redirect URI obtained them.

Jobs are skipped unless you ask for them: their video files live in the cloud
bucket, so imported job cards would point at /media URLs this machine cannot
serve. Pass --jobs if you want the history anyway.

Reads Firestore with your Application Default Credentials. Writes only into
the SQLite file - Firestore is never modified.

    python scripts/import_firestore_to_sqlite.py [--jobs] [--dry-run]
"""

import argparse
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if _root not in sys.path:
    sys.path.append(_root)

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "vvvvv-504116")
# Import the backends directly: the facade would hand us only one of them.
os.environ["MPT_DB"] = "sqlite"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", action="store_true", help="also import job history")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    from app.services import db_firestore as src
    from app.services import db_sqlite as dst

    print(f"source: Firestore ({os.environ['GOOGLE_CLOUD_PROJECT']})")
    print(f"target: {dst.db_path()}")
    if args.dry_run:
        print("DRY RUN - nothing will be written\n")

    settings = src.get_global_settings()
    interesting = [k for k in ("youtube_client_id", "tiktok_client_key",
                               "facebook_app_id", "groq_api_key") if settings.get(k)]
    print(f"\nsettings: {len(settings)} keys (have: {', '.join(interesting) or 'none'})")
    if not args.dry_run:
        dst.save_global_settings(settings)

    users = src.list_users()
    print(f"users: {len(users)}")
    for u in users:
        uid = u["uid"]
        social = u.get("social") or {}
        connected = ", ".join(social.keys()) or "nothing connected"
        print(f"  {u.get('email', uid)} - {connected}")
        if args.dry_run:
            continue
        dst.create_user_if_missing(uid, u.get("email", ""), u.get("provider", ""))
        if u.get("profile"):
            dst.save_user_profile(uid, u["profile"])
        for platform, token in social.items():
            dst.save_user_social(uid, platform, token)

    if args.jobs:
        jobs = src.list_all_jobs()
        print(f"jobs: {len(jobs)} (files stay in the cloud bucket)")
        if not args.dry_run:
            for job in jobs:
                uid = job.pop("uid", "")
                if uid and job.get("id"):
                    dst.create_job(uid, job)
    else:
        print("jobs: skipped (pass --jobs to include history)")

    print("\ndone" if not args.dry_run else "\ndry run complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
