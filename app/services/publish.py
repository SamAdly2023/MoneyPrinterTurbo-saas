"""
Direct social publishing (no third-party aggregator).

- YouTube: full OAuth 2.0 + resumable upload via the YouTube Data API v3.
  Works locally and is free within the daily quota.
- TikTok: OAuth 2.0 + Content Posting API "upload to inbox" (drafts). Direct
  auto-posting requires TikTok to audit your app; until then videos land in
  your TikTok drafts and you tap "Post" in the app.

Only the `requests` library is used, so no Google/TikTok SDKs are required.
OAuth client credentials come from Settings (config.toml); tokens are stored
locally in storage/saas/social.json (private to this machine).
"""

import json
import os
import threading
import time
import urllib.parse

import requests
from loguru import logger

from app.config import config
from app.utils import utils

_SOCIAL_FILE = os.path.join(utils.storage_dir("saas", create=True), "social.json")
_lock = threading.RLock()

YT_SCOPE = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)
TT_SCOPE = "user.info.basic,video.upload"


# --------------------------------------------------------------------------- #
# Token store
# --------------------------------------------------------------------------- #
def _load() -> dict:
    if os.path.isfile(_SOCIAL_FILE):
        try:
            with open(_SOCIAL_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"failed to read social.json: {e}")
    return {}


def _save(data: dict):
    tmp = f"{_SOCIAL_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _SOCIAL_FILE)


def _get(platform: str) -> dict:
    return _load().get(platform, {})


def _set(platform: str, info: dict):
    with _lock:
        data = _load()
        data[platform] = info
        _save(data)


def _clear(platform: str):
    with _lock:
        data = _load()
        data.pop(platform, None)
        _save(data)


def base_url() -> str:
    return (config.app.get("publish_base_url") or "http://localhost:8080").rstrip("/")


def _redirect_uri(platform: str) -> str:
    return f"{base_url()}/api/v1/saas/{platform}/callback"


# --------------------------------------------------------------------------- #
# YouTube
# --------------------------------------------------------------------------- #
def youtube_auth_url() -> str:
    client_id = config.app.get("youtube_client_id", "")
    if not client_id:
        raise ValueError("YouTube client ID is not set. Add it in Settings first.")
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri("youtube"),
        "response_type": "code",
        "scope": YT_SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def youtube_exchange_code(code: str) -> dict:
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": config.app.get("youtube_client_id", ""),
            "client_secret": config.app.get("youtube_client_secret", ""),
            "redirect_uri": _redirect_uri("youtube"),
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
    _set("youtube", info)
    logger.success(f"YouTube connected: {info.get('channel')}")
    return info


def _youtube_refresh() -> str:
    info = _get("youtube")
    if not info.get("refresh_token"):
        raise ValueError("YouTube is not connected")
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": config.app.get("youtube_client_id", ""),
            "client_secret": config.app.get("youtube_client_secret", ""),
            "refresh_token": info["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    tok = resp.json()
    info["access_token"] = tok.get("access_token", "")
    info["expiry"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    _set("youtube", info)
    return info["access_token"]


def _youtube_token() -> str:
    info = _get("youtube")
    if not info:
        raise ValueError("YouTube is not connected")
    if time.time() >= info.get("expiry", 0):
        return _youtube_refresh()
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


def youtube_upload(video_path, title, description, tags, privacy="public") -> dict:
    token = _youtube_token()
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


def youtube_status() -> dict:
    info = _get("youtube")
    return {
        "connected": bool(info.get("refresh_token") or info.get("access_token")),
        "channel": info.get("channel", ""),
    }


# --------------------------------------------------------------------------- #
# TikTok
# --------------------------------------------------------------------------- #
def tiktok_auth_url() -> str:
    client_key = config.app.get("tiktok_client_key", "")
    if not client_key:
        raise ValueError("TikTok client key is not set. Add it in Settings first.")
    params = {
        "client_key": client_key,
        "scope": TT_SCOPE,
        "response_type": "code",
        "redirect_uri": _redirect_uri("tiktok"),
        "state": "mpt",
    }
    return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(params)


def tiktok_exchange_code(code: str) -> dict:
    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": config.app.get("tiktok_client_key", ""),
            "client_secret": config.app.get("tiktok_client_secret", ""),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": _redirect_uri("tiktok"),
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
    _set("tiktok", info)
    logger.success("TikTok connected")
    return info


def _tiktok_refresh() -> str:
    info = _get("tiktok")
    if not info.get("refresh_token"):
        raise ValueError("TikTok is not connected")
    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": config.app.get("tiktok_client_key", ""),
            "client_secret": config.app.get("tiktok_client_secret", ""),
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
    _set("tiktok", info)
    return info["access_token"]


def _tiktok_token() -> str:
    info = _get("tiktok")
    if not info:
        raise ValueError("TikTok is not connected")
    if time.time() >= info.get("expiry", 0):
        return _tiktok_refresh()
    return info["access_token"]


def tiktok_upload(video_path, title="") -> dict:
    """Send the video to the user's TikTok drafts (inbox). No audit required."""
    token = _tiktok_token()
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


def tiktok_status() -> dict:
    info = _get("tiktok")
    return {"connected": bool(info.get("refresh_token") or info.get("access_token"))}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def disconnect(platform: str):
    _clear(platform)


def status() -> dict:
    return {"youtube": youtube_status(), "tiktok": tiktok_status()}


def publish_video(video_path: str, meta: dict, platforms: list) -> dict:
    """Publish one local video file to the requested platforms using its metadata."""
    if not os.path.isfile(video_path):
        return {p: {"success": False, "error": "video file not found"} for p in platforms}
    title = (meta or {}).get("title") or ""
    description = (meta or {}).get("description") or ""
    tags = (meta or {}).get("tags") or []
    results = {}
    if "youtube" in platforms:
        try:
            results["youtube"] = youtube_upload(
                video_path, title, description, tags,
                privacy=config.app.get("youtube_privacy", "public"),
            )
        except Exception as e:  # noqa: BLE001 - surface any API error to the UI
            logger.error(f"youtube publish failed: {e}")
            results["youtube"] = {"success": False, "error": str(e)}
    if "tiktok" in platforms:
        try:
            results["tiktok"] = tiktok_upload(video_path, title)
        except Exception as e:  # noqa: BLE001
            logger.error(f"tiktok publish failed: {e}")
            results["tiktok"] = {"success": False, "error": str(e)}
    return results
