"""Who just signed up, what did they do, and where did they come from?

Run on the server (reads your own database, prints a plain-text report, changes nothing):

    cd ~/moneyprinterturbo
    ~/virtualenv/moneyprinterturbo/3.11/bin/python scripts/new_subscriber_report.py          # newest 3 signups
    ~/virtualenv/moneyprinterturbo/3.11/bin/python scripts/new_subscriber_report.py 6        # newest 6
    ~/virtualenv/moneyprinterturbo/3.11/bin/python scripts/new_subscriber_report.py someone@example.com

About "where did they come from": website visits are recorded anonymously (a random id kept in the
visitor's browser) and sign-ups are NOT linked to that id. So the "likely visit" lines below are a
best guess from timing - a visit that was on the login page shortly before the account was created -
not a certain match. Treat a single clear candidate as probable and several close ones as unknown.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

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

from app.services import firestore_db  # noqa: E402

# A visit counts as a candidate when it was last seen this long before / after the account was created.
WINDOW_BEFORE = timedelta(minutes=45)
WINDOW_AFTER = timedelta(minutes=10)


def parse(ts):
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fmt(ts) -> str:
    d = parse(ts)
    return d.strftime("%Y-%m-%d %H:%M UTC") if d else "-"


def ago(ts) -> str:
    d = parse(ts)
    if not d:
        return ""
    secs = (datetime.now(timezone.utc) - d).total_seconds()
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 172800:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} days ago"


def pageviews(session_id: str) -> list:
    """(path, time) for one visit. Uses the backend's own connection; returns [] if it can't."""
    try:
        backend = firestore_db.backend_name()
        marker = "?" if backend == "sqlite" else "%s"
        conn = firestore_db._connect()
        cur = conn.cursor()
        cur.execute(f"SELECT path, created_at FROM visitor_pageviews WHERE session_id={marker} ORDER BY created_at", (session_id,))
        rows = cur.fetchall()
        return [(r["path"], r["created_at"]) for r in rows]
    except Exception:  # noqa: BLE001 - analytics extras must never break the report
        return []


def payments(uid: str) -> list:
    try:
        backend = firestore_db.backend_name()
        marker = "?" if backend == "sqlite" else "%s"
        cur = firestore_db._connect().cursor()
        cur.execute(f"SELECT payment_id, credits, created_at FROM processed_payments WHERE uid={marker} ORDER BY created_at", (uid,))
        return [(r["payment_id"], r["credits"], r["created_at"]) for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []


def candidate_visits(created, sessions):
    """Visits whose last activity was close to the sign-up, best (closest) first."""
    out = []
    for s in sessions:
        last = parse(s.get("last_seen"))
        if not last:
            continue
        delta = created - last  # positive: visit ended before the account was created
        if -WINDOW_AFTER <= delta <= WINDOW_BEFORE:
            out.append((abs(delta.total_seconds()), s))
    out.sort(key=lambda x: x[0])
    return [s for _, s in out]


def describe_visit(s: dict, indent: str = "      ") -> None:
    where = ", ".join(x for x in (s.get("city"), s.get("region"), s.get("country")) if x) or "unknown location"
    print(f"{indent}source   : {s.get('source_type', 'Direct')}"
          + (f"  (referrer: {s['referrer']})" if s.get("referrer") else "  (no referrer)"))
    utm = {k: s.get(k) for k in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content") if s.get(k)}
    if utm:
        print(f"{indent}campaign : " + ", ".join(f"{k.replace('utm_', '')}={v}" for k, v in utm.items()))
    print(f"{indent}landed on: {s.get('landing_path', '-')}   device: {s.get('device_type', '-')}")
    print(f"{indent}location : {where}" + (f"   ISP: {s['isp']}" if s.get("isp") else "")
          + ("   [proxy/VPN]" if s.get("is_proxy") else "") + ("   [datacentre]" if s.get("is_hosting") else ""))
    pv = pageviews(s["session_id"])
    if pv:
        print(f"{indent}pages    : " + " -> ".join(f"{p} ({fmt(t)[11:16]})" for p, t in pv[:10]))
    span = fmt(s.get("first_seen")) + (f" to {fmt(s.get('last_seen'))}" if s.get("last_seen") else "")
    print(f"{indent}visit    : {span}, {s.get('pageview_count', 0)} pageview(s) before signing up")


def report(user: dict, jobs: list, sessions: list) -> None:
    uid = user["uid"]
    created = parse(user.get("created_at"))
    profile = user.get("profile") or {}
    print("=" * 78)
    print(f"{user.get('email', '(no email)')}   signed up {fmt(user.get('created_at'))}  ({ago(user.get('created_at'))})")
    print("=" * 78)
    print(f"  sign-in method : {user.get('provider', '-')}")
    flags = []
    if user.get("is_disabled"):
        flags.append("DISABLED")
    if user.get("is_admin"):
        flags.append("admin")
    if flags:
        print(f"  flags          : {', '.join(flags)}")

    print("\n  WHAT THEY HAVE")
    print(f"    credits              : {user.get('credits', 0)}")
    print(f"    Streams plan         : {user.get('streams_plan') or 'free'}")
    print(f"    Auto Mode subscribed : {'yes' if user.get('auto_mode_subscription_active') else 'no'}")
    if profile.get("business_name") or profile.get("business_website"):
        print(f"    business             : {profile.get('business_name', '-')}  {profile.get('business_website', '')}")
    connected = [k for k, v in (user.get("social") or {}).items() if isinstance(v, dict) and (v.get("access_token") or v.get("stream_key") or v.get("token"))]
    print(f"    connected accounts   : {', '.join(connected) or 'none'}")
    paid = payments(uid)
    for pid, credits, when in paid:
        print(f"    credit purchase      : {credits} credits on {fmt(when)}")

    channels = profile.get("replay_channels") or []
    print(f"\n  STREAMS ({len(channels)})")
    for c in channels:
        sess = c.get("session") or {}
        print(f"    - {c.get('title', c.get('name', 'untitled'))}  [{c.get('destination', 'youtube')}, "
              f"{'real' if c.get('is_real') else 'draft'}, {c.get('status', '-')}]"
              + (f"  started {fmt(sess.get('started_at'))}, loops {c.get('loop_count', 0)}" if sess.get("started_at") else ""))
    if not channels:
        print("    none created")

    mine = sorted((j for j in jobs if j.get("uid") == uid), key=lambda j: j.get("created_at", ""))
    print(f"\n  VIDEO JOBS ({len(mine)})")
    for j in mine[:15]:
        print(f"    - {fmt(j.get('created_at'))}  {j.get('status', '-'):<10} {j.get('kind', 'generate'):<8} {str(j.get('title', ''))[:48]}"
              + (f"  !! {str(j['error'])[:70]}" if j.get("error") else ""))
    if not mine:
        print("    none")

    recorded = user.get("signup_visit")
    if recorded:
        print("\n  WHERE THEY CAME FROM (recorded when they signed up - certain)")
        describe_visit(recorded, indent="    ")
        print()
        return

    print("\n  WHERE THEY CAME FROM (best guess from timing - this account predates signup tracking)")
    if not created:
        print("    no sign-up time recorded")
        return
    cands = candidate_visits(created, sessions)
    if not cands:
        print("    no recorded visit was on the site in the 45 minutes before sign-up")
        print("    (visitor tracking only covers public pages; a direct link to /login, or an ad blocker, leaves no trace)")
        return
    # only visits that actually reached the login page are strong candidates
    strong = [s for s in cands if any(p.startswith("/login") for p, _ in pageviews(s["session_id"]))] or cands
    print(f"    {len(strong)} candidate visit(s); closest first" + ("  - ONLY ONE, so probably this one" if len(strong) == 1 else ""))
    for i, s in enumerate(strong[:3], 1):
        print(f"    #{i}")
        describe_visit(s)
    print()


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else "3"
    users = sorted(firestore_db.list_users(), key=lambda u: u.get("created_at", ""), reverse=True)
    if "@" in arg:
        users = [u for u in users if arg.lower() in (u.get("email") or "").lower()]
        if not users:
            sys.exit(f"no user matches {arg!r}")
    else:
        users = users[: max(1, int(arg))]
    jobs = firestore_db.list_all_jobs()
    sessions = firestore_db.list_visitor_sessions(limit=3000)
    print(f"{len(firestore_db.list_users())} users in total, {len(sessions)} recent visitor sessions loaded.\n")
    for u in users:
        report(u, jobs, sessions)


if __name__ == "__main__":
    main()
