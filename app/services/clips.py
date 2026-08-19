"""
Long-form-to-shorts AI clipping.

Pipeline: transcribe the uploaded source video with the app's existing
faster-whisper integration (see subtitle.py), ask the configured LLM to pick
highlight segments from the timestamped transcript, then cut + letterbox +
caption each chosen segment into its own vertical short. Each segment becomes
its own SaaS job (see saas.py _process_clip_job) so it flows through the
exact same job list / admin monitor / publish pipeline as a normal generated
video - no separate UI or storage model needed for the output side.
"""

import os

from loguru import logger
from moviepy import ColorClip, CompositeVideoClip, TextClip, VideoFileClip

from app.models.schema import VideoAspect
from app.services import subtitle
from app.services.video import (
    _build_contact_card_clip,
    _build_logo_overlay_clip,
    _get_configured_video_codec,
    _write_videofile_with_codec_fallback,
    wrap_text,
)
from app.utils import utils


def probe_duration(path: str) -> float:
    clip = VideoFileClip(path)
    try:
        return float(clip.duration)
    finally:
        clip.close()


def _srt_time_to_seconds(ts: str) -> float:
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def transcribe_source(video_path: str, work_dir: str) -> list:
    """Extract audio and transcribe it. Returns [(start, end, text), ...] in seconds."""
    os.makedirs(work_dir, exist_ok=True)
    audio_path = os.path.join(work_dir, "audio.wav")
    clip = VideoFileClip(video_path)
    try:
        if clip.audio is None:
            return []
        clip.audio.write_audiofile(audio_path, logger=None)
    finally:
        clip.close()

    subtitle_path = os.path.join(work_dir, "transcript.srt")
    subtitle.create(audio_path, subtitle_path)
    if not os.path.isfile(subtitle_path):
        return []

    items = subtitle.file_to_subtitles(subtitle_path)  # (idx, "start --> end", text)
    segments = []
    for _idx, times, text in items:
        if not text.strip():
            continue
        start_str, end_str = times.split(" --> ")
        segments.append((_srt_time_to_seconds(start_str), _srt_time_to_seconds(end_str), text))
    return segments


def transcript_for_prompt(segments: list, max_chars: int = 6000) -> str:
    lines = []
    for start, _end, text in segments:
        m, s = divmod(int(start), 60)
        lines.append(f"[{m:02d}:{s:02d}] {text}")
    return "\n".join(lines)[:max_chars]


def evenly_spaced_segments(total_duration: float, clip_count: int, clip_len: float = 45.0) -> list:
    """Fallback when the LLM is unavailable or returns nothing usable."""
    clip_count = max(1, clip_count)
    clip_len = min(clip_len, max(15.0, total_duration / clip_count))
    usable = max(total_duration - clip_len, 0)
    step = usable / clip_count if clip_count > 1 else 0
    out = []
    for i in range(clip_count):
        start = min(i * step, usable)
        end = min(start + clip_len, total_duration)
        if end - start < 5:
            continue
        out.append({"start": start, "end": end, "title": f"Highlight {i + 1}"})
    return out


def excerpt_for_window(segments: list, start: float, end: float) -> str:
    return " ".join(text for s, e, text in segments if e > start and s < end).strip()


def render_clip(
    source_path: str, start: float, end: float, out_path: str, *,
    subtitle_enabled: bool, font_name: str, font_size: int,
    subtitle_position: str, text_color: str,
    transcript_segments: list = None,
    logo_path: str = "", contact_website: str = "", contact_phone: str = "",
) -> None:
    clip = VideoFileClip(source_path).subclipped(start, end)
    try:
        target_w, target_h = VideoAspect.portrait.to_resolution()
        clip_ratio = clip.w / clip.h
        target_ratio = target_w / target_h

        if abs(clip_ratio - target_ratio) < 0.01:
            base = clip.resized(new_size=(target_w, target_h))
        else:
            scale = target_w / clip.w if clip_ratio > target_ratio else target_h / clip.h
            new_w, new_h = max(1, int(clip.w * scale)), max(1, int(clip.h * scale))
            background = ColorClip(size=(target_w, target_h), color=(0, 0, 0)).with_duration(clip.duration)
            resized = clip.resized(new_size=(new_w, new_h)).with_position("center")
            base = CompositeVideoClip([background, resized])

        font_path = os.path.join(utils.font_dir(), font_name or "MicrosoftYaHeiBold.ttc")
        if os.name == "nt":
            font_path = font_path.replace("\\", "/")

        layers = [base]
        if logo_path and os.path.isfile(logo_path):
            try:
                layers.append(_build_logo_overlay_clip(logo_path, clip.duration, target_w, target_h))
            except Exception as e:
                logger.warning(f"failed to overlay logo watermark on clip: {e}")
        if contact_website or contact_phone:
            try:
                contact_clip = _build_contact_card_clip(
                    contact_website, contact_phone, clip.duration, target_w, target_h, font_path
                )
                if contact_clip is not None:
                    layers.append(contact_clip)
            except Exception as e:
                logger.warning(f"failed to overlay contact card on clip: {e}")
        if subtitle_enabled and transcript_segments:
            for seg_start, seg_end, text in transcript_segments:
                local_start = max(0.0, seg_start - start)
                local_end = min(clip.duration, seg_end - start)
                if local_end <= local_start:
                    continue
                try:
                    wrapped, _h = wrap_text(
                        text, max_width=int(target_w * 0.9), font=font_path, fontsize=font_size
                    )
                    txt = TextClip(
                        text=wrapped, font=font_path, font_size=font_size, color=text_color,
                        stroke_color="black", stroke_width=max(1, int(font_size * 0.06)),
                        size=(int(target_w * 0.9), None), text_align="center",
                    )
                except Exception as e:
                    logger.warning(f"skipping caption '{text[:30]}...': {e}")
                    continue
                txt = txt.with_start(local_start).with_end(local_end).with_duration(local_end - local_start)
                if subtitle_position == "top":
                    txt = txt.with_position(("center", target_h * 0.05))
                elif subtitle_position == "center":
                    txt = txt.with_position(("center", "center"))
                else:
                    txt = txt.with_position(("center", target_h * 0.88 - txt.h))
                layers.append(txt)

        final = CompositeVideoClip(layers) if len(layers) > 1 else base
        if clip.audio is not None:
            final = final.with_audio(clip.audio)

        _write_videofile_with_codec_fallback(
            final, out_path, codec=_get_configured_video_codec(),
            audio_codec="aac", threads=2, logger=None, fps=30,
        )
        final.close()
    finally:
        clip.close()
