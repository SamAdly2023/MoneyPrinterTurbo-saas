"""
AI talking-avatar video generation via Replicate's hosted SadTalker model -
animates one static presenter photo to lip-sync the exact narration audio
already generated for this video. An alternative "video_source" to stock
footage (material.py) or AI-illustrated visuals (ai_visuals.py), for
businesses that want an actual on-screen presenter instead of B-roll.

Runs on Replicate's pay-per-call API (https://replicate.com/cjwbw/sadtalker)
- no GPU to host ourselves. Cost is per Replicate's own pricing (roughly
$0.08/generation at the time this was written), on top of this app's other
per-video costs (LLM, TTS, etc).

Replicate fetches file inputs over HTTP for anything above ~256kb, which our
narration audio always is - so both the audio and the avatar photo need a
public URL, not a data: URI. We reuse the existing public /media/ trust
model already relied on for Instagram's server-to-server video fetch (see
asgi.py) - the avatar photo lives there permanently (see admin.py's
save_avatar_image), and the narration audio gets a short-lived temp copy
there that's deleted once the prediction finishes.
"""

import os
import shutil
import time

import requests
from loguru import logger

from app.services import firestore_db
from app.services.publish import base_url
from app.utils import utils

REPLICATE_MODEL = "cjwbw/sadtalker"
_POLL_INTERVAL_SECONDS = 3
_MAX_WAIT_SECONDS = 300


def _global() -> dict:
    return firestore_db.get_global_settings()


def _latest_model_version(token: str) -> str:
    """Resolve the model's current version id at call time rather than
    hardcoding one - community models (unlike official ones) require an
    explicit version id, and hardcoding it would go stale whenever the
    model's owner pushes an update."""
    resp = requests.get(
        f"https://api.replicate.com/v1/models/{REPLICATE_MODEL}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    version = (resp.json().get("latest_version") or {}).get("id")
    if not version:
        raise RuntimeError("could not resolve the current SadTalker model version from Replicate")
    return version


def generate_avatar_video(task_id: str, audio_path: str, avatar_photo_path: str) -> list:
    """Returns [video_path] on success - matches the same list[str] contract
    as ai_visuals.generate_ai_materials()/material.download_videos(), so it
    drops into task.py's existing get_video_materials() dispatch unchanged.

    avatar_photo_path is resolved ahead of time by saas.py's
    resolve_avatar_photo_path() (this business's own Profile photo, falling
    back to the admin's platform-wide default) - same resolved-at-job-
    creation pattern already used for logo_path.
    """
    settings = _global()
    token = (settings.get("replicate_api_token") or "").strip()
    if not token:
        raise ValueError("Replicate API token is not set. Ask the admin to add it in Settings.")
    image_path = avatar_photo_path
    if not image_path or not os.path.isfile(image_path):
        raise ValueError("No AI avatar photo uploaded. Upload one in your Profile.")

    output_dir = utils.storage_dir("output", create=True)
    public_audio_name = f"_avatar-src-{task_id}.mp3"
    public_audio_path = os.path.join(output_dir, public_audio_name)
    shutil.copyfile(audio_path, public_audio_path)

    try:
        audio_url = f"{base_url()}/media/{public_audio_name}"
        image_url = f"{base_url()}/media/{os.path.basename(image_path)}"

        version = _latest_model_version(token)
        create_resp = requests.post(
            "https://api.replicate.com/v1/predictions",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "version": version,
                "input": {"source_image": image_url, "driven_audio": audio_url},
            },
            timeout=30,
        )
        create_resp.raise_for_status()
        prediction = create_resp.json()
        get_url = prediction["urls"]["get"]

        waited = 0
        while prediction.get("status") in ("starting", "processing"):
            if waited >= _MAX_WAIT_SECONDS:
                raise TimeoutError("Replicate avatar generation timed out")
            time.sleep(_POLL_INTERVAL_SECONDS)
            waited += _POLL_INTERVAL_SECONDS
            poll_resp = requests.get(get_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
            poll_resp.raise_for_status()
            prediction = poll_resp.json()

        if prediction.get("status") != "succeeded":
            raise RuntimeError(
                f"Replicate avatar generation failed: {prediction.get('error') or prediction.get('status')}"
            )
        video_url = prediction.get("output")
        if not video_url:
            raise RuntimeError("Replicate returned no output video")

        out_path = os.path.join(utils.task_dir(task_id), "avatar.mp4")
        video_resp = requests.get(video_url, timeout=120)
        video_resp.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(video_resp.content)
        logger.success(f"generated avatar video for task {task_id}")
        return [out_path]
    finally:
        if os.path.isfile(public_audio_path):
            os.remove(public_audio_path)
