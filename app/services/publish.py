"""
Direct social publishing (no third-party aggregator), per user.

- YouTube: full OAuth 2.0 + resumable upload via the YouTube Data API v3.
- TikTok: OAuth 2.0 + Content Posting API "upload to inbox" (drafts). Direct
  auto-posting requires TikTok to audit your app; until then videos land in
  your TikTok drafts and you tap "Post" in the app.
- Facebook + Instagram: one Meta OAuth app/connection covers both (Meta Login
  for Business). Facebook Page video upload is a direct multipart upload, no
  public URL needed. Instagram's publish flow requires the video be fetchable
  at a public URL, which is why /media/* is public (see asgi.py) - the
  filenames are unguessable per-job UUIDs, so this is the same "obscurity, not
  a login wall" trust model YouTube/TikTok links already have once published.

OAuth **client** credentials (app-level id/secret) are shared/admin-managed
(app_config/global in Firestore) - every business uses the same registered
OAuth app. The **connected token** for each platform, and the per-business
upload-privacy preference, are per-user. None of this reads the engine's
temporary config-scope overlay (app/services/saas.py's _user_config_scope) -
these functions can be called from a normal HTTP request at any time,
potentially concurrently with the engine processing a *different* user's job.
"""

import json
import os
import time
import urllib.parse

import requests
from loguru import logger

from app.services import firestore_db

YT_SCOPE = (
    "https://www.googleapis.com/auth/youtube.force-ssl "
    "https://www.googleapis.com/auth/youtube.readonly"
)
# force-ssl is a superset of youtube.upload (videos.insert accepts it too),
# and is also what liveBroadcasts/liveStreams write operations require - see
# app/services/live_stream.py. Accounts connected before this change need to
# reconnect to be granted it; see youtube_needs_reconnect() below.
YT_LIVE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
TT_SCOPE = "user.info.basic,video.upload"
FB_SCOPE = "pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish"
META_API = "https://graph.facebook.com/v21.0"

# LinkedIn posts as the signed-in member (w_member_social). openid+profile are
# what /v2/userinfo needs, to learn which person URN to post as.
LI_SCOPE = "openid profile w_member_social"
LI_API = "https://api.linkedin.com/rest"
# Versioned API: the header is mandatory and LinkedIn sunsets versions on a
# rolling ~12-month cycle, so this is a value to bump deliberately rather than
# a detail to leave floating.
LI_VERSION = "202608"


def _global() -> dict:
    return firestore_db.get_global_settings()


def base_url() -> str:
    return (_global().get("publish_base_url") or "http://localhost:8080").rstrip("/")


def _redirect_uri(platform: str) -> str:
    return f"{base_url()}/api/v1/saas/{platform}/callback"


# --------------------------------------------------------------------------- #
# YouTube
# --------------------------------------------------------------------------- #
def _google_token_error(resp, action: str) -> RuntimeError:
    """Google puts the real reason in the token response body, which
    raise_for_status() throws away - so a revoked token and a bad client
    secret both surface as a bare "400". Read the body and say which."""
    try:
        err = resp.json()
    except Exception:
        err = {}
    code = err.get("error", "")
    desc = err.get("error_description", "") or resp.text[:300]
    hints = {
        "invalid_grant": (
            "the refresh token is no longer valid - revoked, unused for 6 months, the "
            "Google account password changed, or the OAuth app is still in Testing mode "
            "(those tokens expire after 7 days). Reconnect YouTube in Settings."
        ),
        "invalid_client": "the YouTube client ID / client secret do not match the Google Cloud OAuth client.",
        "unauthorized_client": "this OAuth client is not authorized for that grant type.",
        "redirect_uri_mismatch": "the redirect URI does not match the one registered in Google Cloud Console.",
    }
    hint = hints.get(code, "")
    msg = f"YouTube {action} failed ({resp.status_code}): {code or 'error'} - {desc}"
    if hint:
        msg += f" | {hint}"
    logger.error(msg)
    return RuntimeError(msg)


def youtube_auth_url() -> str:
    client_id = _global().get("youtube_client_id", "")
    if not client_id:
        raise ValueError("YouTube client ID is not set. Ask the admin to add it in Settings.")
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


def youtube_exchange_code(uid: str, code: str) -> dict:
    settings = _global()
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": settings.get("youtube_client_id", ""),
            "client_secret": settings.get("youtube_client_secret", ""),
            "redirect_uri": _redirect_uri("youtube"),
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if not resp.ok:
        raise _google_token_error(resp, "authorization")
    tok = resp.json()
    info = {
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "expiry": time.time() + int(tok.get("expires_in", 3600)) - 60,
        # Google's authorization-code exchange returns what was actually
        # granted (space-delimited) - a refresh response doesn't reliably
        # include this, so only this path ever sets it. Lets
        # youtube_needs_reconnect() detect a stale-scope token without
        # waiting for a live API call to fail.
        "scope": tok.get("scope", ""),
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
    settings = _global()
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
    if not resp.ok:
        raise _google_token_error(resp, "token refresh")
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


def youtube_access_token(uid: str) -> str:
    """Public accessor for app/services/live_stream.py's Live Streaming API
    calls - refreshes if expired, same as every other YouTube call in this
    module."""
    return _youtube_token(uid)


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
            # Audio/content language is the legitimate signal YouTube uses to
            # match videos to an audience - unlike upload server location
            # (irrelevant to viewers), this actually steers early distribution
            # toward English-speaking markets (US/CA/UK/EU) rather than being
            # left unset and matched more broadly.
            "defaultLanguage": "en-US",
            "defaultAudioLanguage": "en-US",
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
    if not init.ok:
        # requests' default raise_for_status() drops the response body, which is
        # exactly where YouTube puts the actual reason (invalid title, quota,
        # channel not eligible, etc.) - surface it instead of a bare "400".
        logger.error(f"youtube upload init failed ({init.status_code}): {init.text}")
        raise RuntimeError(f"YouTube rejected the upload ({init.status_code}): {init.text[:500]}")
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
    if not up.ok:
        logger.error(f"youtube upload failed ({up.status_code}): {up.text}")
        raise RuntimeError(f"YouTube upload failed ({up.status_code}): {up.text[:500]}")
    vid = up.json().get("id", "")
    logger.success(f"uploaded to YouTube: {vid}")
    return {"success": True, "id": vid, "url": f"https://youtu.be/{vid}" if vid else ""}


def youtube_status(uid: str) -> dict:
    info = firestore_db.get_user_social(uid).get("youtube", {})
    return {
        "connected": bool(info.get("refresh_token") or info.get("access_token")),
        "channel": info.get("channel", ""),
        "needs_reconnect_for_live": youtube_needs_reconnect(uid),
    }


def youtube_needs_reconnect(uid: str) -> bool:
    """True if the stored token predates the live-streaming scope (or was
    never recorded, i.e. connected before the `scope` field existed) - Go
    Live for a real channel needs this checked before attempting any
    liveBroadcasts/liveStreams call, which would otherwise just fail with an
    opaque 403 from Google."""
    info = firestore_db.get_user_social(uid).get("youtube", {})
    if not info.get("refresh_token"):
        return False  # not connected at all - not a "reconnect" case
    granted = set((info.get("scope") or "").split())
    return YT_LIVE_SCOPE not in granted


# --------------------------------------------------------------------------- #
# TikTok
# --------------------------------------------------------------------------- #
def tiktok_auth_url() -> str:
    client_key = _global().get("tiktok_client_key", "")
    if not client_key:
        raise ValueError("TikTok client key is not set. Ask the admin to add it in Settings.")
    params = {
        "client_key": client_key,
        "scope": TT_SCOPE,
        "response_type": "code",
        "redirect_uri": _redirect_uri("tiktok"),
        "state": "mpt",
    }
    return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(params)


def tiktok_exchange_code(uid: str, code: str) -> dict:
    settings = _global()
    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": settings.get("tiktok_client_key", ""),
            "client_secret": settings.get("tiktok_client_secret", ""),
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
    firestore_db.save_user_social(uid, "tiktok", info)
    logger.success(f"TikTok connected for {uid}")
    return info


def _tiktok_refresh(uid: str) -> str:
    info = firestore_db.get_user_social(uid).get("tiktok", {})
    if not info.get("refresh_token"):
        raise ValueError("TikTok is not connected")
    settings = _global()
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
# Facebook + Instagram (one Meta connection covers both)
# --------------------------------------------------------------------------- #
def facebook_auth_url() -> str:
    app_id = _global().get("facebook_app_id", "")
    if not app_id:
        raise ValueError("Facebook App ID is not set. Ask the admin to add it in Settings.")
    params = {
        "client_id": app_id,
        "redirect_uri": _redirect_uri("facebook"),
        "scope": FB_SCOPE,
        "response_type": "code",
    }
    return "https://www.facebook.com/v21.0/dialog/oauth?" + urllib.parse.urlencode(params)


def facebook_exchange_code(uid: str, code: str) -> dict:
    settings = _global()
    app_id = settings.get("facebook_app_id", "")
    app_secret = settings.get("facebook_app_secret", "")

    resp = requests.get(
        f"{META_API}/oauth/access_token",
        params={
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": _redirect_uri("facebook"),
            "code": code,
        },
        timeout=30,
    )
    resp.raise_for_status()
    short_token = resp.json().get("access_token", "")
    if not short_token:
        raise RuntimeError("Facebook did not return an access token")

    # Exchange for a long-lived user token (~60 days) so the connection
    # survives more than the couple hours a short-lived token would.
    long_resp = requests.get(
        f"{META_API}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
        timeout=30,
    )
    long_resp.raise_for_status()
    long_data = long_resp.json()
    user_token = long_data.get("access_token", short_token)
    expires_in = int(long_data.get("expires_in") or 60 * 24 * 3600)

    pages_resp = requests.get(f"{META_API}/me/accounts", params={"access_token": user_token}, timeout=30)
    pages_resp.raise_for_status()
    pages = pages_resp.json().get("data", [])
    if not pages:
        raise RuntimeError("No Facebook Page found on this account - create or get added to a Page first")

    # v1: connect the first Page returned. Most single-business accounts
    # only manage one Page; multi-page selection can follow later.
    page = pages[0]
    page_id = page.get("id", "")
    page_token = page.get("access_token", "")
    page_name = page.get("name", "")

    ig_user_id, ig_username = "", ""
    try:
        ig_resp = requests.get(
            f"{META_API}/{page_id}",
            params={"fields": "instagram_business_account", "access_token": page_token},
            timeout=30,
        )
        ig_resp.raise_for_status()
        ig_account = ig_resp.json().get("instagram_business_account")
        if ig_account:
            ig_user_id = ig_account.get("id", "")
            ig_info = requests.get(
                f"{META_API}/{ig_user_id}", params={"fields": "username", "access_token": page_token}, timeout=30
            )
            if ig_info.ok:
                ig_username = ig_info.json().get("username", "")
    except Exception as e:  # noqa: BLE001 - Facebook connection still succeeds without Instagram
        logger.warning(f"could not resolve linked Instagram account: {e}")

    info = {
        "access_token": page_token,
        "expiry": time.time() + expires_in - 60,
        "page_id": page_id,
        "page_name": page_name,
        "ig_user_id": ig_user_id,
        "ig_username": ig_username,
    }
    firestore_db.save_user_social(uid, "facebook", info)
    logger.success(
        f"Facebook connected for {uid}: {page_name}" + (f" (Instagram: @{ig_username})" if ig_username else "")
    )
    return info


def facebook_status(uid: str) -> dict:
    info = firestore_db.get_user_social(uid).get("facebook", {})
    return {
        "connected": bool(info.get("access_token")),
        "page": info.get("page_name", ""),
        "instagram": info.get("ig_username", ""),
    }


def facebook_upload(uid: str, video_path, title, description) -> dict:
    """Upload to the connected Facebook Page as a video post (direct multipart upload)."""
    info = firestore_db.get_user_social(uid).get("facebook", {})
    if not info.get("access_token"):
        raise ValueError("Facebook is not connected")
    page_id = info["page_id"]
    with open(video_path, "rb") as f:
        resp = requests.post(
            f"https://graph-video.facebook.com/v21.0/{page_id}/videos",
            data={
                "access_token": info["access_token"],
                "title": (title or "Untitled")[:255],
                "description": description or "",
            },
            files={"source": f},
            timeout=900,
        )
    if not resp.ok:
        logger.error(f"facebook upload failed ({resp.status_code}): {resp.text}")
        raise RuntimeError(f"Facebook rejected the upload ({resp.status_code}): {resp.text[:500]}")
    vid = resp.json().get("id", "")
    logger.success(f"uploaded to Facebook Page: {vid}")
    return {"success": True, "id": vid, "url": f"https://www.facebook.com/{vid}" if vid else ""}


def instagram_upload(uid: str, video_public_url: str, caption: str) -> dict:
    """Publish a Reel to the Instagram Business account linked to the connected
    Page. video_public_url must be reachable by Instagram's servers (not a
    local path) - Instagram fetches and processes it asynchronously."""
    info = firestore_db.get_user_social(uid).get("facebook", {})
    ig_user_id = info.get("ig_user_id")
    token = info.get("access_token")
    if not ig_user_id or not token:
        raise ValueError("Instagram is not connected (connect Facebook first - it links automatically)")

    create = requests.post(
        f"{META_API}/{ig_user_id}/media",
        data={
            "video_url": video_public_url,
            "caption": (caption or "")[:2200],
            "media_type": "REELS",
            "access_token": token,
        },
        timeout=60,
    )
    if not create.ok:
        logger.error(f"instagram media create failed ({create.status_code}): {create.text}")
        raise RuntimeError(f"Instagram rejected the video ({create.status_code}): {create.text[:500]}")
    creation_id = create.json().get("id", "")
    if not creation_id:
        raise RuntimeError("Instagram did not return a creation ID")

    for _ in range(30):
        time.sleep(5)
        check = requests.get(
            f"{META_API}/{creation_id}", params={"fields": "status_code", "access_token": token}, timeout=30
        )
        status_code = check.json().get("status_code", "") if check.ok else ""
        if status_code == "FINISHED":
            break
        if status_code == "ERROR":
            raise RuntimeError("Instagram failed to process the video")
    else:
        raise RuntimeError("Instagram video processing timed out")

    publish = requests.post(
        f"{META_API}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": token},
        timeout=60,
    )
    if not publish.ok:
        logger.error(f"instagram publish failed ({publish.status_code}): {publish.text}")
        raise RuntimeError(f"Instagram publish failed ({publish.status_code}): {publish.text[:500]}")
    media_id = publish.json().get("id", "")
    logger.success(f"published to Instagram: {media_id}")
    return {"success": True, "id": media_id}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def disconnect(uid: str, platform: str):
    firestore_db.clear_user_social(uid, platform)


# --------------------------------------------------------------------------- #
# LinkedIn
# --------------------------------------------------------------------------- #
def _li_headers(token: str) -> dict:
    return {
        "Authorization": "Bearer " + token,
        "LinkedIn-Version": LI_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def linkedin_auth_url() -> str:
    client_id = _global().get("linkedin_client_id", "")
    if not client_id:
        raise ValueError("LinkedIn client ID is not set. Ask the admin to add it in Settings.")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _redirect_uri("linkedin"),
        "scope": LI_SCOPE,
        "state": "vidzy",
    }
    return "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(params)


def linkedin_exchange_code(uid: str, code: str) -> dict:
    settings = _global()
    resp = requests.post(
        "https://www.linkedin.com/oauth/v2/accessToken",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri("linkedin"),
            "client_id": settings.get("linkedin_client_id", ""),
            "client_secret": settings.get("linkedin_client_secret", ""),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(
            "LinkedIn rejected the sign-in ({}): {}".format(resp.status_code, resp.text[:300])
        )
    tok = resp.json()
    access_token = tok.get("access_token", "")
    if not access_token:
        raise RuntimeError("LinkedIn did not return an access token")

    # Who are we posting as? /v2/userinfo is the OpenID endpoint; its `sub` is
    # the member id the person URN is built from.
    me = requests.get(
        "https://api.linkedin.com/v2/userinfo",
        headers={"Authorization": "Bearer " + access_token},
        timeout=30,
    )
    if not me.ok:
        raise RuntimeError(
            "LinkedIn profile lookup failed ({}): {}".format(me.status_code, me.text[:200])
        )
    profile = me.json()

    info = {
        "access_token": access_token,
        # Member tokens last ~60 days and only approved partners get refresh
        # tokens, so store the expiry and say so plainly once it lapses.
        "expiry": time.time() + int(tok.get("expires_in", 60 * 24 * 3600)) - 60,
        "person_urn": "urn:li:person:" + str(profile.get("sub", "")),
        "name": profile.get("name", ""),
    }
    firestore_db.save_user_social(uid, "linkedin", info)
    logger.success("LinkedIn connected for {}: {}".format(uid, info["name"]))
    return info


def _linkedin_token(uid: str):
    info = firestore_db.get_user_social(uid).get("linkedin", {})
    token = info.get("access_token", "")
    if not token:
        raise ValueError("LinkedIn is not connected")
    if info.get("expiry", 0) and time.time() > info["expiry"]:
        raise RuntimeError(
            "The LinkedIn connection has expired (member tokens last about 60 days). "
            "Reconnect LinkedIn in the dashboard."
        )
    return token, info.get("person_urn", "")


def linkedin_upload(uid: str, video_path: str, title: str, description: str) -> dict:
    token, owner = _linkedin_token(uid)
    if not owner:
        raise RuntimeError("LinkedIn connection is missing its member id - reconnect LinkedIn")
    size = os.path.getsize(video_path)

    init = requests.post(
        LI_API + "/videos?action=initializeUpload",
        headers=_li_headers(token),
        data=json.dumps({
            "initializeUploadRequest": {
                "owner": owner,
                "fileSizeBytes": size,
                "uploadCaptions": False,
                "uploadThumbnail": False,
            }
        }),
        timeout=60,
    )
    if not init.ok:
        raise RuntimeError(
            "LinkedIn upload init failed ({}): {}".format(init.status_code, init.text[:400])
        )
    value = init.json().get("value", {})
    video_urn = value.get("video", "")
    upload_token = value.get("uploadToken", "")
    instructions = value.get("uploadInstructions", [])
    if not video_urn or not instructions:
        raise RuntimeError("LinkedIn did not return upload instructions")

    # Every part answers with an ETag, and finalizeUpload needs those ETags in
    # the same order as the instructions or the video is reassembled wrong.
    part_ids = []
    with open(video_path, "rb") as f:
        for part in instructions:
            first, last = int(part["firstByte"]), int(part["lastByte"])
            f.seek(first)
            chunk = f.read(last - first + 1)
            put = requests.put(
                part["uploadUrl"],
                headers={"Content-Type": "application/octet-stream"},
                data=chunk,
                timeout=600,
            )
            if not put.ok:
                raise RuntimeError(
                    "LinkedIn chunk upload failed ({}): {}".format(put.status_code, put.text[:200])
                )
            etag = (put.headers.get("etag") or put.headers.get("ETag") or "").strip('"')
            if not etag:
                raise RuntimeError("LinkedIn did not return an ETag for an uploaded part")
            part_ids.append(etag)

    fin = requests.post(
        LI_API + "/videos?action=finalizeUpload",
        headers=_li_headers(token),
        data=json.dumps({
            "finalizeUploadRequest": {
                "video": video_urn,
                "uploadToken": upload_token,
                "uploadedPartIds": part_ids,
            }
        }),
        timeout=120,
    )
    if not fin.ok:
        raise RuntimeError(
            "LinkedIn finalize failed ({}): {}".format(fin.status_code, fin.text[:400])
        )

    post = requests.post(
        LI_API + "/posts",
        headers=_li_headers(token),
        data=json.dumps({
            "author": owner,
            "commentary": description or title or "",
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "content": {"media": {"title": title or "Video", "id": video_urn}},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }),
        timeout=60,
    )
    if not post.ok:
        raise RuntimeError(
            "LinkedIn post failed ({}): {}".format(post.status_code, post.text[:400])
        )

    post_urn = post.headers.get("x-restli-id", "")
    logger.success("posted to LinkedIn: " + (post_urn or video_urn))
    return {
        "success": True,
        "id": post_urn,
        "url": ("https://www.linkedin.com/feed/update/" + post_urn) if post_urn else "",
    }


def linkedin_status(uid: str) -> dict:
    info = firestore_db.get_user_social(uid).get("linkedin", {})
    expired = bool(info.get("expiry")) and time.time() > info["expiry"]
    return {
        "connected": bool(info.get("access_token")) and not expired,
        "expired": expired,
        "name": info.get("name", ""),
    }


# --------------------------------------------------------------------------- #
# Bilibili (local app only)
# --------------------------------------------------------------------------- #
# Bilibili has no upload API available to us: their open platform is aimed at
# Chinese institutions and needs qualifications we cannot supply. What works
# instead is replaying a logged-in browser session's cookies, which is what
# bilibili-api-python does. That is against Bilibili's terms and can get an
# account restricted, so it is deliberately confined to the local app:
#
#   - the dependency is not in requirements.txt, so the Cloud Run image has
#     no way to run it even if the code were reached
#   - bilibili_available() additionally refuses whenever rendering dispatches
#     to Cloud Run, i.e. on the hosted site
#
# Nobody's client account can be exposed by this; only the machine the owner
# runs it on. Install locally with:  pip install bilibili-api-python
#
# Default partition 21 is 日常 (Daily), the general-purpose category. Override
# per-deployment with the bilibili_tid setting.
BILIBILI_DEFAULT_TID = 21


def bilibili_available() -> bool:
    from app.services import render_dispatch

    if render_dispatch.enabled():
        return False
    try:
        import bilibili_api  # noqa: F401
    except Exception:  # noqa: BLE001 - not installed is the normal case
        return False
    return True


def bilibili_save_cookies(uid: str, cookie_blob: str) -> dict:
    """Store the three cookies Bilibili needs, parsed out of a pasted header.

    Asking for a whole `Cookie:` header rather than three separate fields is
    deliberate - copying one line out of DevTools is far less error-prone than
    hunting for three values by name.
    """
    wanted = {"SESSDATA": "", "bili_jct": "", "DedeUserID": "", "buvid3": ""}
    for part in (cookie_blob or "").replace("\n", ";").split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip()
        for name in wanted:
            if key.lower() == name.lower():
                wanted[name] = value

    missing = [k for k in ("SESSDATA", "bili_jct", "DedeUserID") if not wanted[k]]
    if missing:
        raise ValueError(
            "These cookies were missing from what you pasted: " + ", ".join(missing)
            + ". Copy the whole Cookie header from a logged-in bilibili.com request."
        )

    info = {
        "sessdata": wanted["SESSDATA"],
        "bili_jct": wanted["bili_jct"],
        "dedeuserid": wanted["DedeUserID"],
        "buvid3": wanted["buvid3"],
        "saved_at": time.time(),
    }
    firestore_db.save_user_social(uid, "bilibili", info)
    logger.success("Bilibili cookies saved for " + uid)
    return info


def bilibili_upload(uid: str, video_path: str, title: str, description: str, tags: list) -> dict:
    if not bilibili_available():
        raise RuntimeError(
            "Bilibili publishing only runs on the local app, and needs "
            "bilibili-api-python installed."
        )
    info = firestore_db.get_user_social(uid).get("bilibili", {})
    if not info.get("sessdata"):
        raise ValueError("Bilibili is not connected - paste your cookies in the dashboard")

    import asyncio

    from bilibili_api import Credential
    from bilibili_api import video_uploader as vu

    credential = Credential(
        sessdata=info.get("sessdata", ""),
        bili_jct=info.get("bili_jct", ""),
        dedeuserid=info.get("dedeuserid", ""),
        buvid3=info.get("buvid3", "") or None,
    )

    # Bilibili caps the title at 80 characters and rejects the submission
    # outright if it is longer, rather than truncating for you.
    safe_title = (title or "Video")[:80]
    tag_list = [t for t in (tags or []) if t][:10] or ["shorts"]

    meta = vu.VideoMeta(
        tid=int(_global().get("bilibili_tid") or BILIBILI_DEFAULT_TID),
        title=safe_title,
        desc=(description or "")[:2000],
        cover="",
        tags=tag_list,
        original=True,
        no_reprint=True,
    )
    page = vu.VideoUploaderPage(path=video_path, title=safe_title, description=(description or "")[:250])
    uploader = vu.VideoUploader(pages=[page], meta=meta, credential=credential)

    # start() is async and the publish pipeline is synchronous, so give it its
    # own loop rather than assuming one is already running.
    result = asyncio.run(uploader.start())

    bvid = (result or {}).get("bvid", "")
    logger.success("uploaded to Bilibili: " + (bvid or str(result)))
    return {
        "success": True,
        "id": bvid or str((result or {}).get("aid", "")),
        "url": ("https://www.bilibili.com/video/" + bvid) if bvid else "",
    }


def bilibili_status(uid: str) -> dict:
    info = firestore_db.get_user_social(uid).get("bilibili", {})
    return {
        "connected": bool(info.get("sessdata")),
        "available": bilibili_available(),
        "uid": info.get("dedeuserid", ""),
    }


def status(uid: str) -> dict:
    return {
        "youtube": youtube_status(uid),
        "tiktok": tiktok_status(uid),
        "facebook": facebook_status(uid),
        "linkedin": linkedin_status(uid),
        "bilibili": bilibili_status(uid),
    }


def publish_video(uid: str, video_path: str, meta: dict, platforms: list) -> dict:
    """Publish one local video file to the requested platforms using its metadata."""
    if not os.path.isfile(video_path):
        return {p: {"success": False, "error": "video file not found"} for p in platforms}
    title = (meta or {}).get("title") or ""
    description = (meta or {}).get("description") or ""
    tags = (meta or {}).get("tags") or []
    profile = firestore_db.get_user_profile(uid)
    results = {}
    if "youtube" in platforms:
        daily_cap = int(_global().get("youtube_daily_cap", 6) or 0)
        if not firestore_db.reserve_youtube_upload_slot(daily_cap):
            logger.warning(f"youtube daily upload cap ({daily_cap}) reached; skipping publish")
            results["youtube"] = {
                "success": False,
                "error": f"Daily YouTube upload limit reached ({daily_cap}/day - set by your Google Cloud quota). This video wasn't published; it will not auto-retry.",
            }
        else:
            try:
                results["youtube"] = youtube_upload(
                    uid, video_path, title, description, tags,
                    privacy=profile.get("youtube_privacy", "public"),
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
    if "facebook" in platforms:
        try:
            results["facebook"] = facebook_upload(uid, video_path, title, description)
        except Exception as e:  # noqa: BLE001
            logger.error(f"facebook publish failed: {e}")
            results["facebook"] = {"success": False, "error": str(e)}
    if "bilibili" in platforms:
        try:
            results["bilibili"] = bilibili_upload(uid, video_path, title, description, tags)
        except Exception as e:  # noqa: BLE001
            logger.error("bilibili publish failed: {}".format(e))
            results["bilibili"] = {"success": False, "error": str(e)}
    if "linkedin" in platforms:
        try:
            results["linkedin"] = linkedin_upload(uid, video_path, title, description)
        except Exception as e:  # noqa: BLE001
            logger.error("linkedin publish failed: {}".format(e))
            results["linkedin"] = {"success": False, "error": str(e)}
    if "instagram" in platforms:
        try:
            video_url = f"{base_url()}/media/{os.path.basename(video_path)}"
            results["instagram"] = instagram_upload(uid, video_url, description or title)
        except Exception as e:  # noqa: BLE001
            logger.error(f"instagram publish failed: {e}")
            results["instagram"] = {"success": False, "error": str(e)}
    return results
