# Running Vidzy on cPanel

Replaces the Google Cloud Run architecture entirely. What's left from Google:
**Firebase Authentication only** (new project `vidzone-app`, owned by
`samadly728@gmail.com`) — no Firestore, no Cloud Run, no Cloud Build, no
Cloud Storage bucket. Data lives in MySQL via cPanel's own database, files
live on cPanel's own disk, and rendering happens in-process on cPanel's CPU —
the same architecture as the local desktop app, just running on a server
instead of a laptop.

## Environment variables to set on cPanel

| Variable | Value | Why |
|---|---|---|
| `MPT_DB` | `mysql` | Selects `app/services/db_mysql.py` as the data backend |
| `MPT_MYSQL_HOST` | usually `localhost` | cPanel's MySQL is almost always on the same box |
| `MPT_MYSQL_PORT` | `3306` | MySQL default |
| `MPT_MYSQL_DB` | *(from cPanel → MySQL Databases)* | Often prefixed with your cPanel username, e.g. `youruser_vidzy` |
| `MPT_MYSQL_USER` | *(from cPanel → MySQL Databases)* | Same prefix convention |
| `MPT_MYSQL_PASSWORD` | *(the password you set when creating the DB user)* | |
| `GOOGLE_APPLICATION_CREDENTIALS` | full path to `secrets/vidzone-app-firebase-adminsdk.json` on the server | Lets `firebase_admin` verify login tokens against the new Auth-only project |
| `GOOGLE_CLOUD_PROJECT` | `vidzone-app` | |

Leave unset (this is the important part — these already default correctly
for a local-disk, in-process setup):
- `MPT_STORAGE_DIR` — defaults to a `storage/` folder next to the app. That's
  correct here; there's no GCS bucket to point at anymore.
- `MPT_RENDER_MODE` — must stay **unset**. Setting it to `cloudrun_job` (the
  old Cloud Run value) would make the app try to dispatch renders to a Cloud
  Run job that no longer exists. Unset means "render right here," which is
  what you want.

## One-time setup, in order

1. **cPanel → MySQL Databases**: create a database and a user with full
   privileges on it. Note the three values (db name, username, password) —
   they go into the env vars above.
2. **Get the schema created**: nothing to run by hand — `app/services/db_mysql.py`
   creates its tables automatically the first time the app connects
   (`_init_schema`, called from `_connect()`). Just starting the app with the
   env vars set is enough.
3. **Copy the app's private files onto the server** (never through git):
   - `secrets/vidzone-app-firebase-adminsdk.json` — generated already, ask
     for it directly, never comes via a public channel.
   - `config.toml` — only matters for whatever `MPT_DB=mysql`'s first-run
     seeding step reads *before* the database has its own copy of Settings;
     after the first run, edits happen in the app's Settings page and MySQL
     is authoritative, `config.toml` is no longer read for these values.
4. **Run the data migration once**, from a machine with both Firestore and
   MySQL reachable (see `scripts/migrate_firestore_to_mysql.py`'s own
   docstring for the exact env vars it needs — it needs *both* a Firestore
   credential for `vvvvv-504116` and the MySQL connection details above at
   the same time, which is different from the app's normal run-time need for
   only one or the other).
5. **Start the app** with the env vars from the table above. First request
   will show `data backend: mysql` in the logs if it's wired correctly.

## What's NOT needed anymore

Cloud Run service/job, Cloud Build, Artifact Registry, Cloud Scheduler,
Firebase Hosting, the GCS bucket (`vvvvv-504116-mpt-data`) — none of this is
read or written by the app once `MPT_DB=mysql` and `MPT_RENDER_MODE` is
unset. The old project (`vvvvv-504116`) stays dormant as a safety net, not
deleted, per the plan this doc came out of.

## YouTube — the one piece that needs a manual step

YouTube's OAuth 2.0 client lives inside a specific Google Cloud project's
"Credentials" page and there is no API to create one — this is genuinely
console-only, confirmed by testing every plausible `gcloud`/REST path before
concluding this. Do this once:

1. **console.cloud.google.com/apis/credentials?project=vidzone-app**
2. If prompted, configure the **OAuth consent screen** first (External,
   app name "Vidzy", your support email) — takes a minute.
3. **+ Create Credentials → OAuth client ID → Application type: Web application**
4. Name it anything (e.g. "Vidzy - cPanel")
5. Under **Authorized redirect URIs**, add exactly:
   ```
   https://vidzone.live/api/v1/saas/youtube/callback
   ```
6. Save. Copy the **Client ID** and **Client Secret** it shows you.
7. In Vidzy's admin **Settings** page, paste them into `youtube_client_id`
   / `youtube_client_secret`, replacing the old project's values (which the
   migration script carried over verbatim from Firestore — they won't work
   here, since refresh tokens are scoped to the OAuth client that issued
   them, and that client only exists in the old project).
8. Every previously-connected YouTube account needs to click **Connect**
   again in the dashboard — this is unavoidable, not a bug in the migration.

**TikTok, Facebook, and LinkedIn need none of this.** Their OAuth apps live
outside Google Cloud entirely, on their own developer platforms, so the
credentials the migration script copied from Firestore are already correct
and already-connected accounts keep working — nothing to redo, as long as
`publish_base_url` stays `vidzone.live` (unchanged throughout this move).
