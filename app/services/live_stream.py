"""Real YouTube Live streaming: creates/binds a liveBroadcast + liveStream via
the YouTube Live Streaming API, then pushes video to it with an ffmpeg RTMP(S)
subprocess.

This is real, billable, policy-governed YouTube API activity and a real OS
process - deliberately kept separate from app/services/replay.py's pure
timestamp-based state machine (same split as billing.py vs saas.py). replay.py
calls into this module only from its `is_real` branch; the simulated/demo
path never touches this file.

Needs the broader `youtube.force-ssl` scope (see publish.py's YT_SCOPE) -
accounts connected before that scope existed must reconnect
(publish.youtube_needs_reconnect) or every call here fails with a 403.

No pause/resume primitive exists on YouTube's side: `enableAutoStop` ends the
broadcast within about a minute of the RTMP feed stopping, indistinguishable
from a real end-of-stream - see replay.py's pause()/resume(), which reject
outright for real channels rather than pretend this works.
"""

import os
import signal
import subprocess
import time
from datetime import datetime, timezone

import requests
from loguru import logger

from app.services import publish
from app.utils import utils

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"

# In-memory only - same accepted tradeoff as app/controllers/v1/saas.py's
# _CLIP_UPLOADS dict ("single-process engine"): lost on a Passenger restart.
# The channel's persisted ffmpeg_pid (see replay.py) is the fallback so
# stop() can still reach an orphaned process afterward.
_RUNNING: dict[str, subprocess.Popen] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _yt_headers(uid: str) -> dict:
    return {
        "Authorization": f"Bearer {publish.youtube_access_token(uid)}",
        "Content-Type": "application/json; charset=UTF-8",
    }


def _yt_error(action: str, resp) -> RuntimeError:
    """Same idiom as publish.py's youtube_upload errors - YouTube puts the
    real reason (channel not eligible, quota, bad title, ...) in the JSON
    body, which raise_for_status() would throw away."""
    try:
        message = resp.json().get("error", {}).get("message", "")
    except Exception:
        message = ""
    text = message or resp.text[:500]
    logger.error(f"YouTube Live: {action} failed ({resp.status_code}): {resp.text[:500]}")
    return RuntimeError(f"YouTube rejected {action} ({resp.status_code}): {text}")


def create_broadcast_and_stream(uid: str, title: str, privacy: str = "public") -> dict:
    """liveBroadcasts.insert + liveStreams.insert + liveBroadcasts.bind.

    enableAutoStart/enableAutoStop mean the broadcast goes live on its own
    once real RTMP data arrives at the bound stream, and completes on its
    own about a minute after that data stops - no manual transition() call
    needed for the normal start/stop flow (stop() still makes one, best
    effort, so a user-initiated Stop doesn't wait out that grace window).
    """
    headers = _yt_headers(uid)
    title = (title or "Live Stream").strip()[:100] or "Live Stream"

    broadcast_body = {
        "snippet": {"title": title, "scheduledStartTime": _now_iso()},
        "status": {
            "privacyStatus": privacy if privacy in ("public", "unlisted", "private") else "public",
            "selfDeclaredMadeForKids": False,
        },
        "contentDetails": {"enableAutoStart": True, "enableAutoStop": True},
    }
    b = requests.post(
        f"{YOUTUBE_API}/liveBroadcasts",
        params={"part": "snippet,status,contentDetails"},
        headers=headers, json=broadcast_body, timeout=30,
    )
    if not b.ok:
        raise _yt_error("creating the live broadcast", b)
    broadcast_id = b.json()["id"]

    stream_body = {
        "snippet": {"title": title},
        "cdn": {"frameRate": "variable", "ingestionType": "rtmp", "resolution": "variable"},
    }
    s = requests.post(
        f"{YOUTUBE_API}/liveStreams",
        params={"part": "snippet,cdn,contentDetails"},
        headers=headers, json=stream_body, timeout=30,
    )
    if not s.ok:
        raise _yt_error("creating the live stream", s)
    stream = s.json()
    stream_id = stream["id"]
    ingestion = stream.get("cdn", {}).get("ingestionInfo", {})

    bind = requests.post(
        f"{YOUTUBE_API}/liveBroadcasts/bind",
        params={"id": broadcast_id, "part": "id,contentDetails", "streamId": stream_id},
        headers=headers, timeout=30,
    )
    if not bind.ok:
        raise _yt_error("binding the broadcast to the stream", bind)

    stream_name = ingestion.get("streamName", "")
    return {
        "broadcast_id": broadcast_id,
        "stream_id": stream_id,
        "rtmps_url": f"{ingestion.get('rtmpsIngestionAddress', '')}/{stream_name}",
        "rtmp_url": f"{ingestion.get('ingestionAddress', '')}/{stream_name}",
        "watch_url": f"https://youtube.com/watch?v={broadcast_id}",
    }


def _build_ffmpeg_command(source_path: str, rtmp_target: str, is_job_source: bool, loop: bool) -> list:
    ffmpeg = utils.get_ffmpeg_binary()
    command = [ffmpeg, "-re"]
    if loop:
        command += ["-stream_loop", "-1"]
    command += ["-i", source_path]
    if is_job_source:
        # Our own rendered videos are already H.264/AAC (see video.py's
        # _ENCODE_PRESET / audio_codec) - remux only, near-zero CPU.
        command += ["-c:v", "copy", "-c:a", "copy"]
    else:
        # Unknown codec/container from the secondary upload path - a real
        # re-encode to a conservative RTMP-safe profile, tuned for low
        # latency rather than file-size/quality (video.py's render settings
        # are the wrong tradeoff for a live push).
        command += [
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-b:v", "4500k", "-maxrate", "4500k", "-bufsize", "9000k",
            "-pix_fmt", "yuv420p", "-g", "60",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        ]
    command += ["-f", "flv", rtmp_target]
    return command


def start_push_with_fallback(channel_id: str, source_path: str, rtmps_url: str, rtmp_url: str,
                              is_job_source: bool, loop: bool) -> int:
    """Tries the encrypted RTMPS target first (YouTube's current guidance);
    falls back to plain RTMP if the resolved ffmpeg binary wasn't built with
    TLS support (not guaranteed for the bundled imageio_ffmpeg fallback -
    see utils.get_ffmpeg_binary()) and dies immediately. Returns the pid of
    whichever process is actually running."""
    for target in (rtmps_url, rtmp_url):
        command = _build_ffmpeg_command(source_path, target, is_job_source, loop)
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        time.sleep(3)
        if proc.poll() is None:
            _RUNNING[channel_id] = proc
            logger.success(f"live push started for channel {channel_id}: pid={proc.pid} target={target}")
            return proc.pid
        stderr = (proc.stderr.read() or b"").decode(errors="replace")[:500] if proc.stderr else ""
        logger.warning(f"ffmpeg exited immediately for {target}, trying next option: {stderr}")
    raise RuntimeError("ffmpeg could not start pushing to YouTube - check that ffmpeg is installed correctly")


def is_alive(channel_id: str, pid) -> bool:
    """Prefers the in-memory Popen handle; falls back to a bare OS-level
    existence check via the persisted pid if that's gone (e.g. after a
    Passenger restart orphaned the process)."""
    proc = _RUNNING.get(channel_id)
    if proc is not None:
        return proc.poll() is None
    if not pid:
        return False
    try:
        os.kill(pid, 0)  # signal 0: existence check only, doesn't kill
        return True
    except OSError:
        return False


def stop_push(channel_id: str, pid) -> None:
    proc = _RUNNING.pop(channel_id, None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass  # already dead, or this pid was never ours - nothing to do


def end_broadcast(uid: str, broadcast_id: str) -> None:
    """Best-effort manual transition to "complete" - used by stop() so the
    YouTube-side broadcast doesn't sit through the ~1 minute enableAutoStop
    grace window after a user-initiated Stop. Never raises: ffmpeg is
    already stopped by the time this runs, so a failure here shouldn't block
    the user's Stop action - enableAutoStop will still end it shortly after."""
    try:
        headers = _yt_headers(uid)
        r = requests.post(
            f"{YOUTUBE_API}/liveBroadcasts/transition",
            params={"broadcastStatus": "complete", "id": broadcast_id, "part": "id,status"},
            headers=headers, timeout=30,
        )
        if not r.ok:
            logger.warning(f"could not transition broadcast {broadcast_id} to complete: {r.text[:300]}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"could not transition broadcast {broadcast_id} to complete: {e}")
