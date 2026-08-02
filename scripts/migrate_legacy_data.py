"""
One-off migration: import the pre-Firebase local/GCS data (config.toml API
keys, storage/saas/jobs.json, storage/saas/social.json) into one user's
Firestore doc.

Usage (after that user has signed in at least once via the new login page,
so their Firestore user doc exists):

    python scripts/migrate_legacy_data.py \
        --email samadly728@gmail.com \
        --config-toml path/to/legacy/config.toml \
        --jobs-json path/to/legacy/jobs.json \
        --social-json path/to/legacy/social.json

Pull the legacy files down from the Cloud Run GCS-mounted bucket first, e.g.:
    gcloud storage cp gs://vvvvv-504116-mpt-data/config.toml ./legacy/
    gcloud storage cp gs://vvvvv-504116-mpt-data/storage/saas/jobs.json ./legacy/
    gcloud storage cp gs://vvvvv-504116-mpt-data/storage/saas/social.json ./legacy/
"""

import argparse
import json
import sys

import toml

from app.services import firestore_db

_APP_KEYS = {
    "video_source", "pexels_api_keys", "pixabay_api_keys", "extra_token",
    "llm_provider", "groq_api_key", "groq_model_name", "grok_api_key",
    "grok_model_name", "openai_api_key", "openai_base_url", "openai_model_name",
    "youtube_client_id", "youtube_client_secret", "youtube_privacy",
    "tiktok_client_key", "tiktok_client_secret", "publish_base_url",
    "auto_publish", "auto_publish_platforms",
}
_UI_KEYS = {
    "voice_name", "video_aspect", "subtitle_enabled", "font_size",
    "subtitle_position", "paragraph_number", "video_clip_duration", "bgm_type",
    "font_name", "text_fore_color",
}


def find_uid_by_email(email: str) -> str:
    for user in firestore_db.list_users():
        if (user.get("email") or "").lower() == email.lower():
            return user["uid"]
    raise SystemExit(
        f"No Firestore user found for {email} yet - have them sign in via "
        "the new /login page first, then re-run this script."
    )


def migrate_settings(uid: str, config_toml_path: str):
    data = toml.load(config_toml_path)
    app_cfg = data.get("app", {})
    ui_cfg = data.get("ui", {})

    settings = firestore_db.get_user_settings(uid)
    for k in _APP_KEYS:
        if k in app_cfg:
            settings[k] = app_cfg[k]
    for k in _UI_KEYS:
        if k in ui_cfg:
            settings[k] = ui_cfg[k]
    firestore_db.save_user_settings(uid, settings)
    print(f"imported settings for {uid}")


def migrate_jobs(uid: str, jobs_json_path: str):
    with open(jobs_json_path, encoding="utf-8") as f:
        jobs = json.load(f)
    if not isinstance(jobs, list):
        print("jobs.json is not a list, skipping", file=sys.stderr)
        return
    for job in jobs:
        firestore_db.create_job(uid, job)
    print(f"imported {len(jobs)} job(s) for {uid}")


def migrate_social(uid: str, social_json_path: str):
    with open(social_json_path, encoding="utf-8") as f:
        social = json.load(f)
    for platform, tokens in social.items():
        firestore_db.save_user_social(uid, platform, tokens)
    print(f"imported social tokens for {uid}: {list(social.keys())}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--config-toml")
    parser.add_argument("--jobs-json")
    parser.add_argument("--social-json")
    args = parser.parse_args()

    uid = find_uid_by_email(args.email)
    print(f"migrating legacy data into uid={uid} ({args.email})")

    if args.config_toml:
        migrate_settings(uid, args.config_toml)
    if args.jobs_json:
        migrate_jobs(uid, args.jobs_json)
    if args.social_json:
        migrate_social(uid, args.social_json)

    print("done")


if __name__ == "__main__":
    main()
