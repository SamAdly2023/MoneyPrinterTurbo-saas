"""
Direct social publishing (no third-party aggregator), per user.

- YouTube: full OAuth 2.0 + resumable upload via the YouTube Data API v3.
- TikTok: OAuth 2.0 + Content Posting API "upload to inbox" (drafts). Direct
  auto-posting requires TikTok to audit your app; until then videos land in
  your TikTok drafts and you tap "Post" in the app.

OAuth client credentials and tokens are read/written straight from each
user's Firestore document (never the shared `config` globals) - these
functions can be called from a normal HTTP request at any time, potentially
concurrently with the engine processing a *different* user's job, so they
must not depend on the engine's temporary config-scope overlay.
"""

import json
import os
import time
import urllib.parse

import requests
from loguru import logger

from app.services import firestore_db

YT_SCOPE = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)
TT_SCOPE = "user.info.basic,video.upload"


def _settings(uid: str) -> dict:
    return firestore_db.get_user_settings(uid)


def base_url(uid: str) -> str:
    return (_settings(uid).get("publish_base_url") or "http://localhost:8080").rstrip("/")


def _redirect_uri(uid: str, platform: str) -> str:
    return f"{base_url(uid)}/api/v1/saas/{platform}/callback"


# --------------------------------------------------------------------------- #
# YouTube
# --------------------------------------------------------------------------- #
def youtube_auth_url(uid: str) -> str:
    client_id = _settings(uid).get("youtube_client_id", "")
    if not client_id:
        raise ValueError("YouTube client ID is not set. Add it in Settings first.")
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(uid, "youtube"),
        "response_type": "code",
        "scope": YT_SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def youtube_exchange_code(uid: str, code: str) -> dict:
    settings = _settings(uid)
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": settings.get("youtube_client_id", ""),
            "client_secret": settings.get("youtube_client_secret", ""),
            "redirect_uri": _redirect_uri(uid, "youtube"),
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    tok = resp.json()
    info = {
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "expiry": time.time() + int(tok.get("expires_in", 3600)) - 60,
    }
    try:
        info["channel"] = _youtube_channel_title(info["access_token"])
    except Exception as e:
        logger.warning(f"could not fetch youtube channel name: {e}")
        info["channel"] = ""
    firestore_db.save_user_social(uid, "youtube", info)
    logger.success(f"YouTube connected for {uid}: {info.get('channel')}")
    return info


def _youtube_refresh(uid: str) -> str:
    info = firestore_db.get_user_social(uid).get("youtube", {})
    if not info.get("refresh_token"):
        raise ValueError("YouTube is not connected")
    settings = _settings(uid)
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": settings.get("youtube_client_id", ""),
            "client_secret": settings.get("youtube_client_secret", ""),
            "refresh_token": info["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    tok = resp.json()
    info["access_token"] = tok.get("access_token", "")
    info["expiry"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    firestore_db.save_user_social(uid, "youtube", info)
    return info["access_token"]


def _youtube_token(uid: str) -> str:
    info = firestore_db.get_user_social(uid).get("youtube", {})
    if not info:
        raise ValueError("YouTube is not connected")
    if time.time() >= info.get("expiry", 0):
        return _youtube_refresh(uid)
    return info["access_token"]


def _youtube_channel_title(token: str) -> str:
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return items[0]["snippet"]["title"] if items else ""


def youtube_upload(uid: str, video_path, title, description, tags, privacy="public") -> dict:
    token = _youtube_token(uid)
    size = os.path.getsize(video_path)
    metadata = {
        "snippet": {
            "title": (title or "Untitled")[:100],
            "description": (description or "")[:5000],
            "tags": (tags or [])[:30],
            "categoryId": "22",  # People & Blogs
        },
        "status": {
            "privacyStatus": privacy if privacy in ("public", "private", "unlisted") else "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/*",
            "X-Upload-Content-Length": str(size),
        },
        data=json.dumps(metadata),
        timeout=60,
    )
    init.raise_for_status()
    location = init.headers.get("Location")
    if not location:
        raise RuntimeError("YouTube did not return an upload URL")
    with open(video_path, "rb") as f:
        up = requests.put(
            location,
            headers={"Content-Type": "video/*", "Content-Length": str(size)},
            data=f,
            timeout=900,
        )
    up.raise_for_status()
    vid = up.json().get("id", "")
    logger.success(f"uploaded to YouTube: {vid}")
    return {"success": True, "id": vid, "url": f"https://youtu.be/{vid}" if vid else ""}


def youtube_status(uid: str) -> dict:
    info = firestore_db.get_user_social(uid).get("youtube", {})
    return {
        "connected": bool(info.get("refresh_token") or info.get("access_token")),
        "channel": info.get("channel", ""),
    }


# --------------------------------------------------------------------------- #
# TikTok
# --------------------------------------------------------------------------- #
def tiktok_auth_url(uid: str) -> str:
    client_key = _settings(uid).get("tiktok_client_key", "")
    if not client_key:
        raise ValueError("TikTok client key is not set. Add it in Settings first.")
    params = {
        "client_key": client_key,
        "scope": TT_SCOPE,
        "response_type": "code",
        "redirect_uri": _redirect_uri(uid, "tiktok"),
        "state": "mpt",
    }
    return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(params)


def tiktok_exchange_code(uid: str, code: str) -> dict:
    settings = _settings(uid)
    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": settings.get("tiktok_client_key", ""),
            "client_secret": settings.get("tiktok_client_secret", ""),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": _redirect_uri(uid, "tiktok"),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    tok = resp.json()
    if tok.get("error"):
        raise RuntimeError(tok.get("error_description") or tok.get("error"))
    info = {
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "open_id": tok.get("open_id", ""),
        "expiry": time.time() + int(tok.get("expires_in", 86400)) - 60,
    }
    firestore_db.save_user_social(uid, "tiktok", info)
    logger.success(f"TikTok connected for {uid}")
    return info


def _tiktok_refresh(uid: str) -> str:
    info = firestore_db.get_user_social(uid).get("tiktok", {})
    if not info.get("refresh_token"):
        raise ValueError("TikTok is not connected")
    settings = _settings(uid)
    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": settings.get("tiktok_client_key", ""),
            "client_secret": settings.get("tiktok_client_secret", ""),
            "grant_type": "refresh_token",
            "refresh_token": info["refresh_token"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    tok = resp.json()
    info["access_token"] = tok.get("access_token", "")
    info["refresh_token"] = tok.get("refresh_token", info["refresh_token"])
    info["expiry"] = time.time() + int(tok.get("expires_in", 86400)) - 60
    firestore_db.save_user_social(uid, "tiktok", info)
    return info["access_token"]


def _tiktok_token(uid: str) -> str:
    info = firestore_db.get_user_social(uid).get("tiktok", {})
    if not info:
        raise ValueError("TikTok is not connected")
    if time.time() >= info.get("expiry", 0):
        return _tiktok_refresh(uid)
    return info["access_token"]


def tiktok_upload(uid: str, video_path, title="") -> dict:
    """Send the video to the user's TikTok drafts (inbox). No audit required."""
    token = _tiktok_token(uid)
    size = os.path.getsize(video_path)
    init = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        data=json.dumps(
            {
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": size,
                    "total_chunk_count": 1,
                }
            }
        ),
        timeout=60,
    )
    init.raise_for_status()
    payload = init.json()
    if payload.get("error", {}).get("code") not in (None, "ok"):
        raise RuntimeError(payload["error"].get("message", "TikTok init failed"))
    data = payload.get("data", {})
    upload_url = data.get("upload_url")
    if not upload_url:
        raise RuntimeError("TikTok did not return an upload URL")
    with open(video_path, "rb") as f:
        put = requests.put(
            upload_url,
            headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes 0-{size - 1}/{size}",
                "Content-Length": str(size),
            },
            data=f,
            timeout=900,
        )
    put.raise_for_status()
    logger.success("sent video to TikTok drafts")
    return {
        "success": True,
        "publish_id": data.get("publish_id", ""),
        "note": "Sent to your TikTok drafts. Open the TikTok app to add the caption and post.",
    }


def tiktok_status(uid: str) -> dict:
    info = firestore_db.get_user_social(uid).get("tiktok", {})
    return {"connected": bool(info.get("refresh_token") or info.get("access_token"))}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def disconnect(uid: str, platform: str):
    firestore_db.clear_user_social(uid, platform)


def status(uid: str) -> dict:
    return {"youtube": youtube_status(uid), "tiktok": tiktok_status(uid)}


def publish_video(uid: str, video_path: str, meta: dict, platforms: list) -> dict:
    """Publish one local video file to the requested platforms using its metadata."""
    if not os.path.isfile(video_path):
        return {p: {"success": False, "error": "video file not found"} for p in platforms}
    title = (meta or {}).get("title") or ""
    description = (meta or {}).get("description") or ""
    tags = (meta or {}).get("tags") or []
    settings = _settings(uid)
    results = {}
    if "youtube" in platforms:
        try:
            results["youtube"] = youtube_upload(
                uid, video_path, title, description, tags,
                privacy=settings.get("youtube_privacy", "public"),
            )
        except Exception as e:  # noqa: BLE001 - surface any API error to the UI
            logger.error(f"youtube publish failed: {e}")
            results["youtube"] = {"success": False, "error": str(e)}
    if "tiktok" in platforms:
        try:
            results["tiktok"] = tiktok_upload(uid, video_path, title)
        except Exception as e:  # noqa: BLE001
            logger.error(f"tiktok publish failed: {e}")
            results["tiktok"] = {"success": False, "error": str(e)}
    return results
