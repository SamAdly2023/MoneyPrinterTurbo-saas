"""24/7 Replay Channel - looped playback of the user's own videos, either
simulated (demo) or real (an actual YouTube Live broadcast).

Every channel is framed against the user's real, already-connected YouTube
account (see app/services/publish.py). A demo channel (`is_real=False`) never
does real RTMP ingestion or calls the YouTube Live Streaming API - "Go Live"
just starts a timestamp-based simulation. A real channel (`is_real=True`)
delegates to app/services/live_stream.py to actually create/bind a YouTube
liveBroadcast+liveStream and push video to it via ffmpeg.

Elapsed time and loop count are always recomputed from persisted timestamps
(never an in-memory counter), so a page refresh, a second tab, or a server
restart all agree on the same numbers - see _recompute() below. This holds
for real channels too; the only extra thing _recompute() does for a real,
live channel is check whether its ffmpeg process is still actually running
(live_stream.is_alive()) and auto-end it if not.

Storage: a small JSON blob per user (profile["replay_channels"]), the same
shallow-merge pattern app/services/db_*.py already use for every other
per-user preference (auto_publish_platforms, logo_path, etc.). No dedicated
SQL table, unlike the PayPal credits system - there is no money involved and
no cross-user query need, so the lighter pattern is proportionate here.
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import UploadFile

from app.services import clips, firestore_db, live_stream, publish, saas
from app.utils import utils

STATUS_IDLE = "idle"
STATUS_LIVE = "live"
STATUS_PAUSED = "paused"
STATUS_ENDED = "ended"

REPLAY_MODES = {"loop", "once"}
OUTPUT_FORMATS = {"9:16", "16:9", "1:1"}
LAYOUTS = {"spotlight", "speaker", "grid"}

ALLOWED_UPLOAD_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # a short "on-loop" source video, not a raw long-form upload
_UPLOAD_PREFIX = "_replay-src-"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def _resolve_source_path(video_url: str) -> str:
    return os.path.join(saas.output_dir(), os.path.basename(video_url))


# --------------------------------------------------------------------------- #
# Sources: the user's own finished videos, plus a secondary upload path
# --------------------------------------------------------------------------- #
def list_sources(uid: str) -> list[dict]:
    """Candidate videos from the user's own job history - finished jobs with
    a rendered video, newest first. Feeds the "create channel from an
    existing video" picker."""
    jobs = [j for j in saas.store.all(uid) if j.get("status") == saas.STATUS_DONE and j.get("videos")]
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return [
        {
            "job_id": j["id"],
            "title": j.get("title") or "Untitled video",
            "video_url": j["videos"][0],
            "created_at": j.get("created_at", ""),
        }
        for j in jobs
    ]


def save_replay_upload(uid: str, file: UploadFile) -> dict:
    """Secondary path: upload a fresh video, independent of any job. Same
    chunked-write-with-cap idiom as saas.py's upload_clip_source, but (like
    upload_avatar_photo) writes into saas.output_dir() so it's immediately
    servable at /media/<filename> with zero extra routing."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_UPLOAD_EXTS:
        raise ValueError(f"unsupported file type: {ext or 'unknown'}")

    filename = f"{_UPLOAD_PREFIX}{utils.get_uuid()}{ext}"
    dest_path = os.path.join(saas.output_dir(), filename)
    size = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError("file too large (max 500MB)")
                out.write(chunk)
        if size == 0:
            raise ValueError("uploaded file is empty")
        duration = clips.probe_duration(dest_path)
    except Exception:
        if os.path.isfile(dest_path):
            os.remove(dest_path)
        raise

    return {"video_url": f"/media/{filename}", "duration_seconds": duration}


# --------------------------------------------------------------------------- #
# Channel CRUD - profile["replay_channels"] is always read/written whole
# --------------------------------------------------------------------------- #
def _load(uid: str):
    profile = firestore_db.get_user_profile(uid)
    channels = list(profile.get("replay_channels") or [])
    return profile, channels


def _save(uid: str, profile: dict, channels: list[dict]) -> None:
    profile["replay_channels"] = channels
    firestore_db.save_user_profile(uid, profile)


def _find(channels: list[dict], channel_id: str):
    for i, c in enumerate(channels):
        if c["id"] == channel_id:
            return i, c
    return -1, None


def list_channels(uid: str) -> list[dict]:
    profile, channels = _load(uid)
    changed = False
    for c in channels:
        if _recompute(c):
            changed = True
    if changed:
        _save(uid, profile, channels)
    return channels


def get_channel(uid: str, channel_id: str) -> dict | None:
    profile, channels = _load(uid)
    idx, channel = _find(channels, channel_id)
    if channel is None:
        return None
    if _recompute(channel):
        _save(uid, profile, channels)
    return channel


def create_channel(
    uid: str,
    name: str,
    source_kind: str,
    source_ref: str,
    replay_mode: str = "loop",
    output_format: str = "9:16",
    layout: str = "spotlight",
    is_real: bool = False,
) -> dict:
    name = (name or "").strip() or "My First Stream"
    if source_kind not in ("job", "upload"):
        raise ValueError("source_kind must be 'job' or 'upload'")
    replay_mode = replay_mode or "loop"
    output_format = output_format or "9:16"
    layout = layout or "spotlight"
    if replay_mode not in REPLAY_MODES:
        raise ValueError(f"unknown replay_mode: {replay_mode}")
    if output_format not in OUTPUT_FORMATS:
        raise ValueError(f"unknown output_format: {output_format}")
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout: {layout}")

    source_job_id = None
    if source_kind == "job":
        job = saas.store.get(uid, source_ref)
        if not job or job.get("status") != saas.STATUS_DONE or not job.get("videos"):
            raise ValueError("that video isn't finished rendering yet")
        source_job_id = job["id"]
        video_url = job["videos"][0]
        duration_seconds = clips.probe_duration(_resolve_source_path(video_url))
    else:
        video_url = source_ref
        if not video_url:
            raise ValueError("upload a video first")
        path = _resolve_source_path(video_url)
        if not os.path.isfile(path):
            raise ValueError("uploaded video not found - try uploading again")
        duration_seconds = clips.probe_duration(path)

    now = _now_iso()
    channel = {
        "id": utils.get_uuid(),
        "name": name,
        "source_kind": source_kind,
        "source_job_id": source_job_id,
        "source_video_url": video_url,
        "duration_seconds": duration_seconds,
        "replay_mode": replay_mode,
        "output_format": output_format,
        "layout": layout,
        "status": STATUS_IDLE,
        "created_at": now,
        "updated_at": now,
        "is_real": bool(is_real),
        # Only ever set for a real channel, by go_live() in live_stream.py's
        # branch below - None for demo channels and before the first real
        # Go Live.
        "youtube_broadcast_id": None,
        "youtube_stream_id": None,
        "youtube_watch_url": None,
        "ffmpeg_pid": None,
        "session": {
            "started_at": None,
            "paused_at": None,
            "accumulated_paused_seconds": 0.0,
            "ended_at": None,
            "ended_reason": None,
            "destination_platform": "youtube",
            "destination_label": "",
        },
        "elapsed_seconds": 0.0,
        "loop_count": 1,
    }
    profile, channels = _load(uid)
    channels.append(channel)
    _save(uid, profile, channels)
    return channel


def update_channel(uid: str, channel_id: str, **changes) -> dict:
    profile, channels = _load(uid)
    idx, channel = _find(channels, channel_id)
    if channel is None:
        raise ValueError("channel not found")
    if channel["status"] in (STATUS_LIVE, STATUS_PAUSED):
        raise ValueError("stop the broadcast before changing its settings")

    if "name" in changes and changes["name"] is not None:
        channel["name"] = (changes["name"] or "").strip() or channel["name"]
    if "replay_mode" in changes and changes["replay_mode"] is not None:
        if changes["replay_mode"] not in REPLAY_MODES:
            raise ValueError(f"unknown replay_mode: {changes['replay_mode']}")
        channel["replay_mode"] = changes["replay_mode"]
    if "output_format" in changes and changes["output_format"] is not None:
        if changes["output_format"] not in OUTPUT_FORMATS:
            raise ValueError(f"unknown output_format: {changes['output_format']}")
        channel["output_format"] = changes["output_format"]
    if "layout" in changes and changes["layout"] is not None:
        if changes["layout"] not in LAYOUTS:
            raise ValueError(f"unknown layout: {changes['layout']}")
        channel["layout"] = changes["layout"]
    channel["updated_at"] = _now_iso()

    _save(uid, profile, channels)
    return channel


def delete_channel(uid: str, channel_id: str) -> None:
    profile, channels = _load(uid)
    idx, channel = _find(channels, channel_id)
    if channel is None:
        raise ValueError("channel not found")
    if channel["status"] in (STATUS_LIVE, STATUS_PAUSED):
        raise ValueError("stop the broadcast before deleting this channel")

    if channel["source_kind"] == "upload":
        filename = os.path.basename(channel["source_video_url"])
        if filename.startswith(_UPLOAD_PREFIX):
            path = os.path.join(saas.output_dir(), filename)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    channels.pop(idx)
    _save(uid, profile, channels)


# --------------------------------------------------------------------------- #
# Broadcast state machine - see _recompute() for the one timer formula
# --------------------------------------------------------------------------- #
def go_live(uid: str, channel_id: str) -> dict:
    profile, channels = _load(uid)
    idx, channel = _find(channels, channel_id)
    if channel is None:
        raise ValueError("channel not found")
    if channel["status"] in (STATUS_LIVE, STATUS_PAUSED):
        raise ValueError("this channel is already broadcasting")

    path = _resolve_source_path(channel["source_video_url"])
    if not os.path.isfile(path):
        raise ValueError("source video is missing - it may have been deleted")

    youtube = publish.status(uid).get("youtube", {})
    if not youtube.get("connected"):
        raise ValueError("connect YouTube before going live")

    if channel.get("is_real"):
        if publish.youtube_needs_reconnect(uid):
            raise ValueError(
                "Your YouTube connection needs to be renewed for live streaming - "
                "reconnect YouTube (Settings or the dashboard's YouTube button) and try again."
            )
        try:
            result = live_stream.create_broadcast_and_stream(uid, channel["name"])
            pid = live_stream.start_push_with_fallback(
                channel["id"], path, result["rtmps_url"], result["rtmp_url"],
                is_job_source=(channel["source_kind"] == "job"),
                loop=(channel["replay_mode"] == "loop"),
            )
        except RuntimeError as e:
            raise ValueError(
                f"Couldn't start the live stream: {e}. If this is a new channel, make sure "
                "live streaming is enabled for it (phone-verified, no recent restrictions) - "
                "see https://support.google.com/youtube/answer/2474026."
            )
        channel["youtube_broadcast_id"] = result["broadcast_id"]
        channel["youtube_stream_id"] = result["stream_id"]
        channel["youtube_watch_url"] = result["watch_url"]
        channel["ffmpeg_pid"] = pid

    now = _now_iso()
    channel["status"] = STATUS_LIVE
    channel["session"] = {
        "started_at": now,
        "paused_at": None,
        "accumulated_paused_seconds": 0.0,
        "ended_at": None,
        "ended_reason": None,
        "destination_platform": "youtube",
        "destination_label": youtube.get("channel") or "",
    }
    channel["updated_at"] = now
    _recompute(channel)
    _save(uid, profile, channels)
    return channel


def pause(uid: str, channel_id: str) -> dict:
    profile, channels = _load(uid)
    idx, channel = _find(channels, channel_id)
    if channel is None:
        raise ValueError("channel not found")
    if channel["status"] != STATUS_LIVE:
        raise ValueError("channel isn't live")
    if channel.get("is_real"):
        raise ValueError(
            "Real YouTube Live broadcasts can't be paused - YouTube has no pause "
            "primitive; stopping the feed just ends the stream. Use Stop, then Go "
            "Live again to start a new broadcast."
        )

    channel["session"]["paused_at"] = _now_iso()
    channel["status"] = STATUS_PAUSED
    channel["updated_at"] = _now_iso()
    _recompute(channel)
    _save(uid, profile, channels)
    return channel


def resume(uid: str, channel_id: str) -> dict:
    profile, channels = _load(uid)
    idx, channel = _find(channels, channel_id)
    if channel is None:
        raise ValueError("channel not found")
    if channel["status"] != STATUS_PAUSED:
        raise ValueError("channel isn't paused")
    if channel.get("is_real"):
        # Real channels never reach STATUS_PAUSED (pause() rejects them
        # first) - this branch only guards against a stale/corrupted record.
        raise ValueError("real YouTube Live broadcasts can't be paused or resumed")

    session = channel["session"]
    paused_at = _parse_iso(session["paused_at"])
    if paused_at is not None:
        session["accumulated_paused_seconds"] += (_now() - paused_at).total_seconds()
    session["paused_at"] = None
    channel["status"] = STATUS_LIVE
    channel["updated_at"] = _now_iso()
    _recompute(channel)
    _save(uid, profile, channels)
    return channel


def stop(uid: str, channel_id: str) -> dict:
    profile, channels = _load(uid)
    idx, channel = _find(channels, channel_id)
    if channel is None:
        raise ValueError("channel not found")
    if channel["status"] not in (STATUS_LIVE, STATUS_PAUSED):
        raise ValueError("channel isn't broadcasting")

    if channel.get("is_real"):
        live_stream.stop_push(channel["id"], channel.get("ffmpeg_pid"))
        if channel.get("youtube_broadcast_id"):
            live_stream.end_broadcast(uid, channel["youtube_broadcast_id"])

    _recompute(channel)
    session = channel["session"]
    if channel["status"] != STATUS_ENDED:
        session["ended_at"] = _now_iso()
        session["ended_reason"] = "manual"
        channel["status"] = STATUS_ENDED
    channel["updated_at"] = _now_iso()
    channel["summary"] = {
        "duration_seconds": channel["elapsed_seconds"],
        "loop_count": channel["loop_count"],
        "destination_platform": session.get("destination_platform", "youtube"),
        "destination_label": session.get("destination_label", ""),
        "ended_reason": session.get("ended_reason"),
    }
    _save(uid, profile, channels)
    return channel


def _recompute(channel: dict) -> bool:
    """The one place elapsed time / loop count get computed, always fresh
    from persisted timestamps - never an in-memory counter. Mutates channel
    in place (adds/refreshes elapsed_seconds/loop_count), returns True if it
    auto-transitioned the channel to 'ended' (caller must persist)."""
    status = channel["status"]
    session = channel["session"]
    duration = max(0.001, float(channel.get("duration_seconds") or 0.001))
    started_at = _parse_iso(session.get("started_at"))
    changed = False

    if status == STATUS_IDLE or started_at is None:
        channel["elapsed_seconds"] = 0.0
        channel["loop_count"] = 1
        return False

    if status == STATUS_LIVE and channel.get("is_real") and not live_stream.is_alive(channel["id"], channel.get("ffmpeg_pid")):
        # The ffmpeg push died on its own - a crash, a network drop, the
        # host killing a long-running process, or (for replay_mode=="once")
        # simply reaching EOF and exiting cleanly. Either way the broadcast
        # is no longer actually live; reflect that rather than showing
        # "live" for a stream that stopped streaming. Unlike the once-mode
        # demo case below, there's no way to know the exact moment it died,
        # only that it's confirmed dead by now - "now" is the best available
        # ended_at.
        duration_reached = (
            (_now() - started_at).total_seconds() - session["accumulated_paused_seconds"] >= duration
        )
        ended_at = _now()
        session["ended_at"] = ended_at.isoformat()
        session["ended_reason"] = "completed" if (channel["replay_mode"] == "once" and duration_reached) else "process_stopped"
        channel["status"] = STATUS_ENDED
        status = STATUS_ENDED
        changed = True
        just_ended_real = True
    else:
        just_ended_real = False

    if status == STATUS_LIVE:
        raw = (_now() - started_at).total_seconds() - session["accumulated_paused_seconds"]
    elif status == STATUS_PAUSED:
        paused_at = _parse_iso(session.get("paused_at")) or _now()
        raw = (paused_at - started_at).total_seconds() - session["accumulated_paused_seconds"]
    else:  # ended
        ended_at = _parse_iso(session.get("ended_at")) or _now()
        raw = (ended_at - started_at).total_seconds() - session["accumulated_paused_seconds"]

    elapsed_seconds = max(0.0, raw)

    if channel["replay_mode"] == "once":
        if status == STATUS_LIVE and elapsed_seconds >= duration:
            # Deterministic end time, not "now" - so every subsequent read
            # (any tab, any time later) recomputes the identical value.
            ended_at = started_at + timedelta(seconds=duration + session["accumulated_paused_seconds"])
            session["ended_at"] = ended_at.isoformat()
            session["ended_reason"] = "completed"
            channel["status"] = STATUS_ENDED
            channel["summary"] = {
                "duration_seconds": duration,
                "loop_count": 1,
                "destination_platform": session.get("destination_platform", "youtube"),
                "destination_label": session.get("destination_label", ""),
                "ended_reason": "completed",
            }
            changed = True
        elapsed_seconds = min(elapsed_seconds, duration)
        loop_count = 1
    else:
        loop_count = int(elapsed_seconds // duration) + 1

    channel["elapsed_seconds"] = round(elapsed_seconds, 1)
    channel["loop_count"] = loop_count
    if just_ended_real:
        channel["summary"] = {
            "duration_seconds": channel["elapsed_seconds"],
            "loop_count": loop_count,
            "destination_platform": session.get("destination_platform", "youtube"),
            "destination_label": session.get("destination_label", ""),
            "ended_reason": session.get("ended_reason"),
        }
    return changed
