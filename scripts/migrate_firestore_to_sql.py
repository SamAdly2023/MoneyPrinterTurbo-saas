"""One-time copy: Firestore (vvvvv-504116) -> MySQL or PostgreSQL (cPanel).

Part of the move off Google Cloud entirely: Firebase stays for Auth only
(new project), while Firestore's data - users, jobs, settings, invites,
visitor analytics - moves into whichever SQL database cPanel offers.

Reads Firestore via the existing db_firestore.py module directly (source is
always Firestore, regardless of MPT_DB), and writes through the
firestore_db.py facade (target follows MPT_DB, same as the live app) - so the
exact same create/save/update logic this script exercises is the logic the
app uses afterward, on whichever backend you point it at. Idempotent: safe to
re-run - both db_mysql.py and db_postgres.py upsert on conflict, so running
this twice just overwrites with the same data rather than erroring or
duplicating.

Firebase Auth UIDs are the join key throughout. Run this only AFTER
`firebase auth:export` / `auth:import` have copied the users into the new
Auth project with the same UIDs - otherwise every document here lands under a
uid nothing can sign in as.

Usage (MySQL):
    export MPT_DB=mysql
    export MPT_MYSQL_HOST=...   MPT_MYSQL_PORT=3306
    export MPT_MYSQL_DB=...     MPT_MYSQL_USER=...   MPT_MYSQL_PASSWORD=...
    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/a/vvvvv-504116/key.json
    python scripts/migrate_firestore_to_sql.py [--dry-run]

Usage (PostgreSQL): same, but MPT_DB=postgres and MPT_PG_HOST/PORT/DB/USER/PASSWORD.
"""

import argparse
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if _root not in sys.path:
    sys.path.append(_root)

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "vvvvv-504116")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing to the target")
    args = ap.parse_args()

    from app.services import db_firestore as src
    from app.services import firestore_db as dst  # facade: routes to MPT_DB's backend

    if dst.backend_name() == "firestore":
        print("MPT_DB is unset or 'firestore' - set it to 'mysql' or 'postgres' before running this.")
        return 1

    print(f"source: Firestore ({os.environ['GOOGLE_CLOUD_PROJECT']})")
    if not args.dry_run:
        print(f"target: {dst.backend_name()}")
    else:
        print(f"target: {dst.backend_name()} (DRY RUN - nothing will be written)")

    # 1. Global settings - LLM keys, platform OAuth credentials, publish_base_url.
    settings = src.get_global_settings()
    print(f"\nsettings: {len(settings)} keys")
    if not args.dry_run:
        dst.save_global_settings(settings)

    # 2. Users + their jobs. UIDs must already exist in the new Auth project
    #    (see the docstring) for these to mean anything once the app is live.
    users = src.list_users()
    print(f"users: {len(users)}")
    job_total = 0
    for u in users:
        uid = u["uid"]
        jobs = src.list_jobs(uid)
        job_total += len(jobs)
        print(f"  {u.get('email', uid)} - {len(jobs)} job(s)")
        if args.dry_run:
            continue
        dst.create_user_if_missing(uid, u.get("email", ""), u.get("provider", ""))
        if u.get("is_disabled"):
            dst.set_user_disabled(uid, True)
        if u.get("is_admin"):
            dst.set_user_admin(uid, True)
        if u.get("profile"):
            dst.save_user_profile(uid, u["profile"])
        for platform, token in (u.get("social") or {}).items():
            dst.save_user_social(uid, platform, token)
        for job in jobs:
            dst.create_job(uid, job)
    print(f"jobs total: {job_total}")

    # 3. Shared engine state + the YouTube daily-quota counter.
    state = src.get_engine_state()
    print(f"\nengine_state: {state}")
    if not args.dry_run:
        dst.set_engine_state(**state)

    # 4. Invites.
    invites = src.list_invites()
    print(f"invites: {len(invites)}")
    if not args.dry_run:
        for inv in invites:
            dst.create_invite(inv["token"], inv["email"], inv["created_by"])
            if inv.get("used_at"):
                # create_invite doesn't take used_at/used_by_uid - claim_invite
                # is the only writer for those, and it insists the email match,
                # which it always will here since we just wrote it ourselves.
                dst.claim_invite(inv["token"], inv["email"], inv.get("used_by_uid", ""))

    # 5. Visitor analytics (marketing-page traffic - admin-only to view, and
    #    the least important thing here, but no reason to drop it).
    sessions = src.list_visitor_sessions(limit=10_000)
    print(f"visitor_sessions: {len(sessions)}")
    if not args.dry_run:
        for s in sessions:
            session_id = s.pop("session_id")
            dst.create_visitor_session(session_id, s)

    print("\ndone" if not args.dry_run else "\ndry run complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
