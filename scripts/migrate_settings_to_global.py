"""
One-off migration for the admin-only Settings / per-business Profile split.

Before this change, each user's Firestore doc had a `settings` field holding
BOTH API keys (Pexels/LLM/OAuth-app credentials) AND per-user toggles
(auto_mode, auto_publish, auto_publish_platforms, youtube_privacy). The code
now reads API keys from a shared `app_config/global` doc and reads a
renamed `profile` field for the per-user toggles + new business branding.

Firestore doesn't rename fields on its own, so any user who already saved
settings before this change still has that data sitting under the old
`settings` key, invisible to the new code. This script:

  1. Copies the technical/API fields from each user's old `settings` into
     app_config/global (first non-empty value across users wins - the
     admin's own values take priority since they're listed first if you
     pass --admin-email).
  2. Copies the per-user toggles (auto_mode, auto_publish,
     auto_publish_platforms, youtube_privacy) from old `settings` into the
     new `profile` field, so nobody's existing preferences silently reset.

Usage:
    python scripts/migrate_settings_to_global.py [--admin-email you@example.com]

Safe to re-run - it only fills in fields that are currently empty/unset in
app_config/global, and merges (doesn't overwrite) each user's profile.
"""

import argparse

from app.services import firestore_db

_GLOBAL_KEYS = {
    "video_source", "pexels_api_keys", "pixabay_api_keys", "extra_token",
    "llm_provider", "groq_api_key", "groq_model_name", "grok_api_key",
    "grok_model_name", "openai_api_key", "openai_base_url", "openai_model_name",
    "youtube_client_id", "youtube_client_secret",
    "tiktok_client_key", "tiktok_client_secret", "publish_base_url",
    "voice_name", "video_aspect", "subtitle_enabled", "font_size",
    "subtitle_position", "paragraph_number", "video_clip_duration", "bgm_type",
    "font_name", "text_fore_color",
}
_PROFILE_KEYS = {"auto_mode", "auto_publish", "auto_publish_platforms", "youtube_privacy"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-email", default="", help="Process this user's old settings first")
    args = parser.parse_args()

    users = firestore_db.list_users()
    if args.admin_email:
        users.sort(key=lambda u: (u.get("email", "").lower() != args.admin_email.lower()))

    global_settings = firestore_db.get_global_settings()
    global_changed = False

    for user in users:
        uid = user["uid"]
        old_settings = user.get("settings") or {}
        if not old_settings:
            continue

        for k in _GLOBAL_KEYS:
            if k in old_settings and not global_settings.get(k):
                global_settings[k] = old_settings[k]
                global_changed = True

        profile_updates = {k: old_settings[k] for k in _PROFILE_KEYS if k in old_settings}
        if profile_updates:
            profile = firestore_db.get_user_profile(uid)
            profile.update(profile_updates)
            firestore_db.save_user_profile(uid, profile)
            print(f"merged old per-user toggles into profile for {user.get('email', uid)}")

    if global_changed:
        firestore_db.save_global_settings(global_settings)
        print("updated app_config/global with API keys found in old per-user settings")
    else:
        print("no old per-user API keys found to migrate")


if __name__ == "__main__":
    main()
