"""
AI-generated visuals: an alternative video source to stock footage.

Uses Pollinations.ai's free, keyless image generation API to create a custom
illustration per search term, then animates each into a short Ken-Burns
(slow zoom) clip - the same technique already used for local image materials
in video.py's preprocess_video(). Returns a List[str] of local .mp4 paths,
matching material.download_videos()'s return shape exactly, so it drops into
the existing get_video_materials -> combine_videos pipeline unchanged.

This is an admin-selectable alternative under Settings -> "Primary video
source", not a replacement - stock footage stays available and is still the
default, since AI generation is slower and depends on a free third-party
service with no uptime guarantee.
"""

import os
import random
import time
import urllib.parse

import requests
from loguru import logger
from moviepy import CompositeVideoClip, ImageClip

from app.config import config
from app.models.schema import VideoAspect
from app.services.video import _get_configured_video_codec, _write_videofile_with_codec_fallback
from app.utils import utils

DEFAULT_STYLE = "cinematic photo, dramatic lighting, high detail, professional photography, 4k"
_MIN_VALID_IMAGE_BYTES = 3000  # guards against tiny error/placeholder responses


def _image_url(prompt: str, width: int, height: int, seed: int) -> str:
    base = (config.app.get("pollinations_base_url_image") or "https://image.pollinations.ai/prompt").rstrip("/")
    encoded = urllib.parse.quote(prompt, safe="")
    query = urllib.parse.urlencode({"width": width, "height": height, "nologo": "true", "seed": seed})
    return f"{base}/{encoded}?{query}"


def _styled_prompt(term: str) -> str:
    style = (config.app.get("ai_visual_style") or "").strip() or DEFAULT_STYLE
    return f"{term}, {style}"


def _download_image(url: str, dest_path: str, timeout: int = 45, max_retries: int = 3) -> bool:
    # Pollinations is a free, keyless service with no documented rate limit -
    # under this app's load it started returning bursts of 429s with every
    # prior attempt retried instantly (zero backoff), which just kept
    # tripping the limiter. A short exponential backoff on 429 specifically
    # (not on other errors, which are more likely permanent) recovers most
    # of those without meaningfully slowing down a single job.
    delay = 2
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 429:
                logger.warning(
                    f"AI image request rate-limited (attempt {attempt + 1}/{max_retries}), "
                    f"backing off {delay}s"
                )
                time.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                f.write(resp.content)
            return os.path.getsize(dest_path) > _MIN_VALID_IMAGE_BYTES
        except Exception as e:  # noqa: BLE001 - caller falls back to the next term/attempt
            logger.warning(f"AI image request failed: {e}")
            return False
    return False


def _animate_image(image_path: str, out_path: str, duration: float, fps: int = 30):
    """Static image -> short clip with a slow zoom, same technique as the
    local-image path in video.py's preprocess_video()."""
    clip = ImageClip(image_path).with_duration(duration).with_position("center")
    zoom_clip = clip.resized(lambda t: 1 + (duration * 0.03) * (t / max(duration, 0.01)))
    final_clip = CompositeVideoClip([zoom_clip])
    try:
        _write_videofile_with_codec_fallback(
            final_clip, out_path, codec=_get_configured_video_codec(), logger=None, fps=fps
        )
    finally:
        final_clip.close()
        clip.close()


def generate_ai_materials(
    task_id: str, search_terms: list, video_aspect: str,
    max_clip_duration: int = 5, count_needed: int = 6,
) -> list:
    """Generate `count_needed` short AI-illustrated clips, cycling through
    search_terms for prompt variety. Returns [] if the service is entirely
    unreachable so the caller can fail the job cleanly instead of silently
    shipping a video with no visuals."""
    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()
    out_dir = utils.task_dir(task_id)
    terms = [t for t in (search_terms or []) if t] or ["abstract background"]

    paths = []
    attempts = 0
    max_attempts = count_needed * 3  # tolerate some failed generations without looping forever
    while len(paths) < count_needed and attempts < max_attempts:
        term = terms[attempts % len(terms)]
        attempts += 1
        seed = random.randint(1, 999_999)
        img_path = os.path.join(out_dir, f"ai-img-{len(paths) + 1}.jpg")
        if not _download_image(_image_url(_styled_prompt(term), video_width, video_height, seed), img_path):
            continue
        # A small gap between successful requests too, so a single job doesn't
        # itself become the thing that trips the rate limit for the next image.
        time.sleep(0.6)
        clip_path = os.path.join(out_dir, f"ai-clip-{len(paths) + 1}.mp4")
        try:
            _animate_image(img_path, clip_path, duration=max_clip_duration)
            paths.append(clip_path)
        except Exception as e:  # noqa: BLE001 - try the next term rather than failing the whole job
            logger.warning(f"failed to animate AI image for '{term}': {e}")
        finally:
            if os.path.isfile(img_path):
                os.remove(img_path)

    if paths:
        logger.success(f"generated {len(paths)} AI visual clips ({attempts} attempts)")
    else:
        logger.error("AI visual generation produced no usable clips")
    return paths
