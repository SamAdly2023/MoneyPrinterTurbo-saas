"""
SaaS layer for MoneyPrinterTurbo (multi-tenant).

Adds a persistent, self-running "video creation engine" on top of the existing
generation pipeline (app/services/task.py):

    - A Firestore-backed job store, scoped per user (users/{uid}/jobs/{id}).
    - A single background worker that runs everyone's saved scripts one-by-one,
      strictly sequentially (a fair global FIFO across every signed-up user).
    - Generated videos are copied into a local output folder.

Per-user API keys / defaults: `material.py`, `voice.py`, and `llm.py` all read
their settings directly from the global `app.config.config` module at the
point of use (not via function parameters). Since the engine processes
exactly one job at a time, `_user_config_scope()` below temporarily overlays
that job's owner's Firestore-stored settings onto the shared config globals
for the duration of that one job, then restores the previous values in a
`finally` block. This is only safe because processing is strictly sequential
- if the engine is ever parallelized, this must be revisited.
"""

import contextlib
import copy
import json
import os
import random
import re
import shutil
import threading
import time
from datetime import datetime, timezone

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import MaterialInfo, VideoParams
from app.services import clips
from app.services import firestore_db
from app.services import llm
from app.services import publish
from app.services import render_dispatch
from app.services import state as sm
from app.services import task as tm
from app.utils import utils

# Job lifecycle statuses used by the dashboard.
# Once a video is live on every platform the user asked for, the copy in our
# bucket is dead weight - the platforms host it now. Off by default because it
# also removes the Download button from that job's card.
DELETE_AFTER_PUBLISH = os.getenv("MPT_DELETE_AFTER_PUBLISH", "").strip().lower() in ("1", "true", "yes")

# Auto Mode belongs where rendering is free: the machine running the engine
# in-process. On Cloud Run the service only dispatches jobs, so every
# auto-generated video costs money - the button is hidden there and the
# endpoint refuses to turn it on. MPT_ALLOW_AUTO_MODE overrides either way.
_allow_auto = os.getenv("MPT_ALLOW_AUTO_MODE", "").strip().lower()

# Even locally, generation needs a ceiling. The engine loop generates whenever
# the queue is empty, so without this it produces videos continuously for as
# long as it runs - and auto-publishes every one of them.
AUTO_DAILY_LIMIT = max(0, int(os.getenv("MPT_AUTO_DAILY_LIMIT", "3")))
_auto_quota = {"day": "", "count": 0}


def auto_mode_available() -> bool:
    if _allow_auto in ("1", "true", "yes"):
        return True
    if _allow_auto in ("0", "false", "no"):
        return False
    return not render_dispatch.enabled()

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# Admin-managed, shared by every user's jobs (app_config/global in Firestore).
APP_STR_KEYS = {
    "video_source", "ai_visual_style", "auto_topic_template", "extra_token", "llm_provider",
    "groq_api_key", "groq_model_name", "grok_api_key", "grok_model_name",
    "openai_api_key", "openai_base_url", "openai_model_name",
    "youtube_client_id", "youtube_client_secret",
    "tiktok_client_key", "tiktok_client_secret",
    "facebook_app_id", "facebook_app_secret", "publish_base_url",
}
APP_LIST_KEYS = {"pexels_api_keys", "pixabay_api_keys"}
UI_KEYS = {
    "voice_name", "video_aspect", "subtitle_enabled", "font_size",
    "subtitle_position", "paragraph_number", "video_clip_duration", "bgm_type",
    "font_name", "text_fore_color",
}
# Per-business (this job's owner's own profile), not shared.
PROFILE_APP_KEYS = {"youtube_privacy"}
APP_OTHER_KEYS = {"auto_publish", "auto_publish_platforms"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def output_dir() -> str:
    """Folder where finished videos are collected for the user."""
    return utils.storage_dir("output", create=True)


class JobStore:
    """Thin, uid-scoped wrapper around the Firestore job collections."""

    def all(self, uid: str):
        return firestore_db.list_jobs(uid)

    def all_admin(self):
        """Every job across every user - admin dashboard only."""
        return firestore_db.list_all_jobs()

    def get(self, uid: str, job_id: str):
        return firestore_db.get_job(uid, job_id)

    def add(self, uid: str, job: dict):
        return firestore_db.create_job(uid, job)

    def update(self, uid: str, job_id: str, **changes):
        firestore_db.update_job(uid, job_id, **changes)
        return firestore_db.get_job(uid, job_id)

    def delete(self, uid: str, job_id: str):
        firestore_db.delete_job(uid, job_id)

    def next_pending(self, worker_id: str):
        """Atomically claim the oldest pending job across every user: (uid, job) or None."""
        return firestore_db.claim_next_pending_job(worker_id)

    def reset_stuck_jobs(self):
        firestore_db.reset_stuck_jobs()


store = JobStore()


def create_job(uid: str, title: str, params: dict, auto: bool = False, kind: str = "generate") -> dict:
    job = {
        "id": utils.get_uuid(),
        "kind": kind,
        "title": (title or "").strip() or "Untitled video",
        "params": params,
        "status": STATUS_PENDING,
        "progress": 0,
        "videos": [],
        "error": "",
        "task_id": "",
        "auto": auto,
    }
    job = store.add(uid, job)
    # In cloudrun_job mode the API does not render anything itself - it starts
    # a Job execution that does. If that call fails for any reason we fall
    # back to the in-process engine rather than leaving the job stranded.
    if not render_dispatch.trigger():
        engine.wake()
    logger.info(f"queued {kind} job {job['id']} for {uid} - {job['title']}")
    return job


def uploads_dir() -> str:
    """Folder where user-uploaded long-form videos wait to be clipped."""
    return utils.storage_dir("uploads", create=True)


def materials_dir() -> str:
    """Folder for user-uploaded photos/videos used as a job's own visuals
    (video_source == "local") - the same "local_videos" directory
    video.py's preprocess_video() already resolves material.url against."""
    return utils.storage_dir("local_videos", create=True)


def logos_dir() -> str:
    """Folder where each business's uploaded logo watermark lives."""
    return utils.storage_dir("logos", create=True)


def resolve_logo_path(profile: dict, use_logo: bool) -> str:
    """Absolute path to this business's logo file, or "" if not usable.

    Centralized here so the manual "New Script" flow (saas.py controller's
    _build_params) and Auto Mode (build_default_params below) resolve the
    same logo the same way instead of duplicating this fallback logic.
    """
    if not use_logo:
        return ""
    rel = (profile or {}).get("logo_path") or ""
    if not rel:
        return ""
    abs_path = os.path.join(logos_dir(), rel)
    return abs_path if os.path.isfile(abs_path) else ""


def resolve_avatar_photo_path(profile: dict) -> str:
    """Absolute path to the AI-avatar presenter photo to use for this job:
    this business's own Profile photo if they've uploaded one, else the
    admin's platform-wide default - "" if neither exists.

    Unlike the logo (composited locally by moviepy), this file must be
    fetchable over HTTP by Replicate's servers, so both the per-user and
    admin-default photos live in the public output dir (see
    app/controllers/v1/saas.py's upload_avatar_photo and admin.py's
    upload_avatar_image), not the private logos_dir().
    """
    out_dir = output_dir()
    rel = (profile or {}).get("avatar_image_path") or ""
    if rel:
        abs_path = os.path.join(out_dir, rel)
        if os.path.isfile(abs_path):
            return abs_path
    admin_rel = (firestore_db.get_global_settings().get("avatar_image_path") or "").strip()
    if admin_rel:
        abs_path = os.path.join(out_dir, admin_rel)
        if os.path.isfile(abs_path):
            return abs_path
    return ""


def queue_clip_jobs(uid: str, source_path: str, segments: list, transcript_segments: list) -> list:
    """Queue one job per chosen highlight segment - each is a self-contained
    "clip" job (see Engine._process_clip_job) that flows through the same
    job list / admin monitor / publish pipeline as a normal generated video.
    transcript_segments is the full [(start, end, text), ...] transcript of
    the uploaded source video.
    """
    profile = firestore_db.get_user_profile(uid)
    logo_path = resolve_logo_path(profile, profile.get("use_logo", False))
    contact_website, contact_phone = contact_card_fields(profile)

    jobs = []
    for seg in segments:
        local_transcript = [
            [s, e, t] for s, e, t in transcript_segments if e > seg["start"] and s < seg["end"]
        ]
        params = {
            "source_path": source_path,
            "start": seg["start"],
            "end": seg["end"],
            "transcript_excerpt": clips.excerpt_for_window(transcript_segments, seg["start"], seg["end"]),
            "transcript_segments": local_transcript,
            "logo_path": logo_path,
            "contact_website": contact_website,
            "contact_phone": contact_phone,
        }
        job = create_job(uid, title=seg.get("title") or "Highlight clip", params=params, kind="clip")
        jobs.append(job)
    return jobs


# --------------------------------------------------------------------------- #
# Per-user config scoping
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _user_config_scope(uid: str):
    """Overlay the shared global settings + this job owner's business profile
    onto config.app/config.ui for the duration of the block.

    The overlay is per-THREAD (see ScopedConfigDict in app/config/config.py),
    not a mutation of one shared global dict - concurrent render workers on
    different threads (multiple Engine workers, or a request thread doing a
    one-off LLM call while a job renders) each see only their own overlaid
    settings, so one job's config can never leak into another's mid-render.
    Yields the owner's profile dict (business name/site/bio etc.) for prompt
    injection.
    """
    global_settings = firestore_db.get_global_settings()
    profile = firestore_db.get_user_profile(uid)

    overlay_app = {}
    for k in APP_STR_KEYS:
        overlay_app[k] = global_settings.get(k, "")
    for k in APP_LIST_KEYS:
        v = global_settings.get(k) or []
        overlay_app[k] = [v] if isinstance(v, str) and v else (v if isinstance(v, list) else [])
    for k in PROFILE_APP_KEYS:
        overlay_app[k] = profile.get(k, "")
    for k in APP_OTHER_KEYS:
        overlay_app[k] = profile.get(k, False if k == "auto_publish" else [])
    overlay_ui = {k: global_settings[k] for k in UI_KEYS if k in global_settings}

    config.app.set_overlay(overlay_app)
    config.ui.set_overlay(overlay_ui)
    try:
        yield profile
    finally:
        config.app.clear_overlay()
        config.ui.clear_overlay()


def _business_context_prompt(profile: dict) -> str:
    """Short instruction block steering generated scripts/metadata toward one
    business's branding. Empty if no business_name is set, so unbranded/admin
    test jobs aren't forced to mention a business that doesn't exist."""
    name = (profile or {}).get("business_name", "").strip()
    if not name:
        return ""
    website = (profile.get("business_website") or "").strip()
    email = (profile.get("business_email") or "").strip()
    address = (profile.get("business_address") or "").strip()
    bio = (profile.get("business_bio") or "").strip()
    contact_bits = [b for b in (website, email, address) if b]
    lines = [f'This video is being made for the business "{name}".']
    if contact_bits:
        lines.append("Contact: " + ", ".join(contact_bits) + ".")
    if bio:
        lines.append(f"About the business: {bio}.")
    lines.append(
        "Where it fits naturally with the video's topic, weave in the business "
        "name and a brief call-to-action mentioning the website or contact info - "
        "the way a real local-business ad would, not as a forced disclaimer."
    )
    return " ".join(lines)


def _business_niche_label(profile: dict) -> str:
    """Short 'a business called X (bio)' phrase for steering visual search
    terms toward the business's actual trade - deliberately terser than
    _business_context_prompt above (which is written for narration flavor
    and CTAs, not a compact instruction to feed a keyword-picking prompt)."""
    profile = profile or {}
    name = (profile.get("business_name") or "").strip()
    bio = (profile.get("business_bio") or "").strip()
    if name and bio:
        return f'a business called "{name}" ({bio})'
    if name:
        return f'a business called "{name}"'
    if bio:
        return f"a business described as: {bio}"
    return ""


def _format_website_display(url: str) -> str:
    """'https://example.com/path' -> 'www.example.com' for a clean on-screen CTA."""
    url = (url or "").strip()
    if not url:
        return ""
    url = re.sub(r"^https?://", "", url, flags=re.IGNORECASE).split("/")[0].strip()
    if not url:
        return ""
    return url if url.lower().startswith("www.") else f"www.{url}"


def contact_card_fields(profile: dict) -> tuple:
    """(website, phone) ready to burn into the end of the video - "" for
    either one this business hasn't set, so that line is simply omitted."""
    profile = profile or {}
    website = _format_website_display(profile.get("business_website", ""))
    phone = (profile.get("business_phone") or "").strip()
    return website, phone


# --------------------------------------------------------------------------- #
# Auto mode: let the user's configured LLM invent viral Shorts and keep the
# queue fed. Each user opts in independently (settings.auto_mode); when the
# global queue is empty the engine round-robins across opted-in users.
# --------------------------------------------------------------------------- #
# Narration length target: ~130-170 words reads aloud in roughly 40-80 seconds.
SCRIPT_LENGTH_PROMPT = (
    "LENGTH REQUIREMENT: the narration MUST be at least 130 words and at most 170 words, "
    "so that read aloud at a normal pace the finished video lasts between 40 and 80 seconds. "
    "Keep writing until you reach at least 130 words - do not stop early. "
    "Output only the spoken narration - no scene directions, headings, emojis or hashtags."
)

# More paragraphs => longer script; 2 lands comfortably in the 40-80s window.
DEFAULT_PARAGRAPHS = 2

AUTO_CATEGORIES = [
    "mind-blowing science facts", "unsolved history mysteries", "psychology tricks",
    "space and the universe", "deep ocean creatures", "future technology",
    "money and wealth secrets", "ancient civilizations", "weird animal facts",
    "the human body", "productivity hacks", "stunning natural wonders",
    "famous unsolved crimes", "food science", "everyday things you use wrong",
    "survival tips", "optical illusions and the brain", "records that seem impossible",
]


def build_default_params(
    subject: str, script: str = "", terms: str = "", profile: dict = None,
    video_source: str = "", video_aspect: str = "", video_materials: list = None,
) -> dict:
    """Build a validated VideoParams dict from the current defaults in config,
    with this user's own video preferences (Profile -> video_aspect /
    subtitle_position / subtitle_pref) overriding the admin's platform
    defaults for their own Auto Mode videos when set.

    video_source/video_aspect, when passed, override the admin's platform
    default for just this one job - used by the "Generate 1 video" dashboard
    button's style/aspect pickers (see controllers/v1/saas.py generate_one).
    video_materials is a list of rel_paths from POST /saas/materials/upload,
    used when that button's style picker is set to "My Media".
    """
    ui = config.ui
    app = config.app
    profile = profile or {}
    script_prompt = SCRIPT_LENGTH_PROMPT
    business_context = _business_context_prompt(profile) if profile else ""
    if business_context:
        script_prompt = script_prompt + " " + business_context

    video_aspect = (
        (video_aspect or "").strip()
        or (profile.get("video_aspect") or "").strip()
        or ui.get("video_aspect", "9:16")
    )
    subtitle_position = (profile.get("subtitle_position") or "").strip() or ui.get("subtitle_position", "bottom")
    subtitle_pref = profile.get("subtitle_pref", "default")
    if subtitle_pref == "on":
        subtitle_enabled = True
    elif subtitle_pref == "off":
        subtitle_enabled = False
    else:
        subtitle_enabled = ui.get("subtitle_enabled", True)

    logo_path = resolve_logo_path(profile, profile.get("use_logo", False))
    contact_website, contact_phone = contact_card_fields(profile)
    avatar_photo_path = resolve_avatar_photo_path(profile)
    materials = (
        [MaterialInfo(provider="local", url=rel_path) for rel_path in video_materials]
        if video_materials else None
    )

    raw = {
        "video_subject": (subject or "").strip(),
        "video_script": (script or "").strip(),
        "video_terms": (terms or "").strip() or None,
        "video_source": (video_source or "").strip() or ("local" if materials else "") or app.get("video_source", "pexels"),
        "video_materials": materials,
        "video_aspect": video_aspect,
        "voice_name": ui.get("voice_name", "en-US-AndrewNeural-Male"),
        "subtitle_enabled": subtitle_enabled,
        "video_clip_duration": ui.get("video_clip_duration", 5),
        "paragraph_number": ui.get("paragraph_number", DEFAULT_PARAGRAPHS),
        "video_count": 1,
        "bgm_type": ui.get("bgm_type", "random"),
        "font_size": ui.get("font_size", 60),
        "subtitle_position": subtitle_position,
        "font_name": ui.get("font_name", "MicrosoftYaHeiBold.ttc"),
        "text_fore_color": ui.get("text_fore_color", "#FFFFFF"),
        # Steers length (and, if set, this business's branding) when the
        # pipeline generates the script from a subject.
        "video_script_prompt": script_prompt,
        "logo_path": logo_path,
        "business_context": _business_niche_label(profile),
        "contact_website": contact_website,
        "contact_phone": contact_phone,
        "avatar_photo_path": avatar_photo_path,
    }
    return VideoParams(**raw).model_dump()


def _parse_idea_json(text: str) -> dict:
    """Extract the first JSON object from an LLM response, tolerating code fences."""
    if not text:
        raise ValueError("empty response")
    cleaned = text.strip()
    # strip markdown code fences if present
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found")
    return json.loads(match.group(0))


def generate_viral_idea(profile: dict = None) -> dict:
    """Ask the configured LLM for one fresh viral Shorts idea + script.

    When the job owner has a business profile set, steer the topic toward
    their own industry (using their bio) instead of a fully random category,
    and weave in their branding/CTA.
    """
    profile = profile or {}
    bio = (profile.get("business_bio") or "").strip()
    if bio:
        topic_line = (
            "Invent ONE fresh, surprising idea directly relevant to this business's "
            f"industry/expertise, described as: {bio}\n\n"
        )
    else:
        category = random.choice(AUTO_CATEGORIES)
        topic_line = f"Invent ONE fresh, surprising idea about: {category}.\n\n"

    topic_template = (config.app.get("auto_topic_template") or "").strip()
    if topic_template:
        topic_line += (
            f'IMPORTANT: the "title" and "subject" MUST start with the exact phrase "{topic_template}" '
            f'(e.g. "{topic_template} X" for whatever specific idea you invent) - every video must follow '
            "this format, no exceptions.\n\n"
        )

    business_context = _business_context_prompt(profile)
    bio_label = _business_niche_label(profile)
    prompt = (
        "You are a viral short-form video scriptwriter for YouTube Shorts, TikTok and Reels.\n"
        + topic_line +
        "Write the NARRATION using this proven viral structure:\n"
        "1) HOOK (the single most important sentence in the whole script - most viewers decide whether "
        "to keep watching within 3 seconds, so this line alone determines whether anyone sees the rest):\n"
        "   - The VERY FIRST WORDS must be the hook itself - no throat-clearing, no 'have you ever', "
        "no 'did you know', no naming the topic before the claim lands.\n"
        "   - State a specific, concrete, counter-intuitive claim or open an information gap the viewer "
        "NEEDS closed (e.g. 'Your memory is lying to you right now', '90% of people fail this in 3 seconds', "
        "'This is why your plants keep dying'). Vague curiosity ('you won't believe what happens next') "
        "does not work - specificity is what stops the scroll.\n"
        "   - Keep it to one short sentence, under 12 words, plain conversational language.\n"
        "2) PAYOFF: 3-4 punchy, specific, surprising facts or steps that deliver on the hook's promise - "
        "no filler sentences between the hook and the first payoff beat.\n"
        "3) CALL TO ACTION: end with a short CTA (e.g. 'Subscribe if you remember the monocle', "
        "'Save this for your next test', 'Follow to see behind the curtain').\n"
        "The narration must be 130-170 words (about 40-80 seconds spoken), conversational and punchy, "
        "with NO emojis, NO hashtags and NO scene directions.\n\n"
        + (business_context + "\n\n" if business_context else "") +
        "Return ONLY valid minified JSON (no markdown, no commentary) with exactly these keys:\n"
        '{"title": "short clean topic label, 3-6 words, no emojis", '
        '"subject": "the core topic in a few words", '
        '"script": "the narration following the HOOK / PAYOFF / CALL TO ACTION structure above", '
        '"keywords": "5-6 comma-separated CONCRETE visual scenes to find stock footage - the FIRST '
        'keyword must visually match the hook line itself so the opening frame reinforces it instead of '
        "showing an unrelated generic scene. "
        + (
            f'CRITICAL: since this video is for {bio_label}, EVERY keyword must depict a scene that is '
            "unmistakably from that same trade/industry (a worker performing the trade, its tools or "
            "materials, a job site, a finished result) - never a generic scene unrelated to it. At the "
            "same time, phrase each one the way real stock-footage libraries label their clips (a common, "
            "widely-filmed scene) rather than obscure trade jargon they are unlikely to carry - e.g. for a "
            'masonry business prefer "bricklayer working, mason repairing wall, construction worker mixing '
            'cement, old brick building" over "tuckpointing tools, grout mixer" (too niche for stock '
            'libraries to have real footage of), '
            if bio_label else 'e.g. \\"monopoly board, darth vader mask, confused crowd, mirror reflection\\", '
        )
        + '"}\n'
        "Make it specific and genuinely interesting; avoid clichés."
    )
    text = llm._generate_response(prompt)
    if not text or str(text).startswith("Error:"):
        raise RuntimeError(f"LLM error: {text}")

    idea = _parse_idea_json(text)
    subject = (idea.get("subject") or idea.get("title") or "").strip()
    script = (idea.get("script") or "").strip()
    title = (idea.get("title") or subject or "Auto video").strip()
    keywords = idea.get("keywords") or ""
    if isinstance(keywords, list):
        keywords = ", ".join(str(k) for k in keywords)
    if not subject and not script:
        raise ValueError("idea missing subject and script")
    if not subject:
        subject = title
    return {"title": title, "subject": subject, "script": script, "keywords": keywords}


def generate_viral_job(
    uid: str, profile: dict = None, video_source: str = "", video_aspect: str = "", video_materials: list = None,
) -> dict:
    idea = generate_viral_idea(profile)
    params = build_default_params(
        idea["subject"], idea["script"], idea["keywords"], profile=profile,
        video_source=video_source, video_aspect=video_aspect, video_materials=video_materials,
    )
    return create_job(uid, title=idea["title"], params=params, auto=True)


def generate_publish_metadata(subject: str, script: str, profile: dict = None, video_aspect: str = "9:16") -> dict:
    """Generate cross-platform publishing metadata (title, description, tags).

    SEO strategy differs by format: Shorts/Reels/TikTok live-or-die on the
    first-second hook and get discovered via the swipe feed, so the title can
    be pure curiosity and the description is hashtag-driven. Landscape
    long-form videos are discovered mainly through YouTube SEARCH, so the
    title and description need the actual searchable keyword up front and
    repeated naturally - hashtags barely matter there.
    """
    is_short = str(video_aspect) == "9:16"
    business_context = _business_context_prompt(profile) if profile else ""

    if is_short:
        format_rules = (
            "This is a SHORT vertical video for YouTube Shorts, TikTok, Instagram Reels and Facebook Reels - "
            "discovered by swiping a feed, not by searching, so lead with curiosity.\n"
            "- title: catchy and curiosity-driven, under 90 characters, ending with 1-2 relevant emojis "
            "(e.g. 'The Mandela Effect Proves Reality is Broken \U0001F633\U0001F300').\n"
            "- description: 1-2 short punchy sentences that hook the viewer, then a space, then 5-7 hashtags "
            "that start with #shorts (e.g. '...why do we all remember it wrong? #shorts #mandelaeffect #mindblown').\n"
            "- tags: 7-10 SEO keyword phrases that can be multiple words, lowercase, WITHOUT the # symbol "
            "(e.g. 'mandela effect', 'glitch in the matrix', 'false memories').\n"
        )
    else:
        format_rules = (
            "This is a LANDSCAPE long-form video, discovered mainly through YouTube SEARCH rather than a "
            "swipe feed, so it needs real on-page SEO, not just curiosity.\n"
            "- title: put the single most-searched keyword phrase for this topic within the first 50 "
            "characters, still compelling to click, under 70 characters total, no emoji spam (at most one).\n"
            "- description: 120-200 words. First line must stand alone as a compelling, keyword-inclusive "
            "summary (it's what shows in search results before 'more'). Then 2-3 short paragraphs "
            "naturally covering the topic and repeating the main keyword phrase 2-3 times total (never "
            "keyword-stuff), ending with a clear subscribe call-to-action. Finish with 3-5 relevant "
            "hashtags (not #shorts).\n"
            "- tags: 10-15 SEO keyword phrases real viewers would type into YouTube search for this topic, "
            "lowercase, WITHOUT the # symbol, ordered most-specific first.\n"
        )

    prompt = (
        "You are a YouTube/social SEO strategist writing publishing metadata for a video.\n"
        f"Video topic: {subject}\n"
        f"Narration: {script}\n\n"
        + (business_context + "\n\n" if business_context else "") +
        format_rules + "\n"
        "Return ONLY valid minified JSON (no markdown) with exactly these keys:\n"
        '{"title": "...", "description": "...", "tags": ["...", "..."]}'
    )
    text = llm._generate_response(prompt)
    if not text or str(text).startswith("Error:"):
        raise RuntimeError(f"LLM error: {text}")
    data = _parse_idea_json(text)
    title = (data.get("title") or subject or "").strip()
    description = (data.get("description") or "").strip()
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,\n]", tags)]
    tags = [str(t).lstrip("#").strip() for t in tags if str(t).strip()]
    return {"title": title, "description": description, "tags": tags}


def _write_publish_file(job_id: str, meta: dict) -> str:
    """Write a human-readable publishing kit next to the video; return its URL."""
    name = f"{job_id}_publish.txt"
    path = os.path.join(output_dir(), name)
    hashtags = " ".join("#" + t.replace(" ", "") for t in meta.get("tags", []))
    body = (
        "TITLE\n" + meta.get("title", "") + "\n\n"
        "DESCRIPTION\n" + meta.get("description", "") + "\n\n"
        "TAGS\n" + ", ".join(meta.get("tags", [])) + "\n\n"
        "HASHTAGS\n" + hashtags + "\n"
    )
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return f"/media/{name}"
    except Exception as e:
        logger.warning(f"failed to write publish file for {job_id}: {e}")
        return ""


def _collect_outputs(job_id: str, task_id: str, result: dict):
    """Copy final videos into the local output folder and return public URLs."""
    videos = (result or {}).get("videos") or []
    out = output_dir()
    public_urls = []
    for idx, src in enumerate(videos, start=1):
        if not isinstance(src, str) or not os.path.isfile(src):
            continue
        ext = os.path.splitext(src)[1] or ".mp4"
        dst_name = f"{job_id}_{idx}{ext}"
        dst = os.path.join(out, dst_name)
        try:
            shutil.copyfile(src, dst)
            public_urls.append(f"/media/{dst_name}")
        except Exception as e:
            logger.warning(f"failed to copy output {src}: {e}")
    return public_urls


class Engine:
    """Background render engine.

    Runs NUM_WORKERS worker threads, each independently claiming and
    processing jobs from the shared Firestore queue - so up to NUM_WORKERS
    videos can render concurrently instead of strictly one at a time
    platform-wide. Safe to do now that:
      - job claiming is an atomic Firestore transaction (firestore_db.
        claim_next_pending_job) - two workers can never grab the same job.
      - per-job settings live in a per-THREAD config overlay (see
        _user_config_scope / ScopedConfigDict), not one shared mutable dict.
      - pause/resume and the auto-mode kill switch live in Firestore (see
        get_engine_state/set_engine_state), not per-worker memory, so an
        admin's toggle applies to every worker (and every Cloud Run
        instance, if ever scaled beyond one) at once.
    """

    NUM_WORKERS = max(1, int(os.getenv("MPT_ENGINE_WORKERS", "2")))

    def __init__(self):
        self._wake = threading.Event()
        self._threads = []
        self._started = False

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        if self._started:
            return
        self._started = True
        store.reset_stuck_jobs()
        if render_dispatch.enabled():
            # Rendering happens in a Cloud Run Job. Starting worker threads
            # here would defeat the point: they are what forced this service
            # to keep CPU allocated 24/7 in the first place.
            logger.success("render mode: cloud run jobs (no in-process workers)")
            return
        for i in range(self.NUM_WORKERS):
            worker_id = f"{utils.get_uuid(remove_hyphen=True)[:8]}-w{i}"
            t = threading.Thread(target=self._run, args=(worker_id,), name=f"saas-engine-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        logger.success(f"video creation engine started with {self.NUM_WORKERS} worker(s)")

    def wake(self):
        self._wake.set()

    def pause(self):
        firestore_db.set_engine_state(paused=True)

    def resume(self):
        firestore_db.set_engine_state(paused=False)
        self.wake()

    def auto_kill_start(self):
        """Admin-only global kill switch: stop ALL users' auto-mode generation."""
        firestore_db.set_engine_state(auto_killed=True)

    def auto_kill_stop(self):
        firestore_db.set_engine_state(auto_killed=False)
        self.wake()

    @property
    def paused(self) -> bool:
        return firestore_db.get_engine_state()["paused"]

    @property
    def auto_killed(self) -> bool:
        return firestore_db.get_engine_state()["auto_killed"]

    def status(self) -> dict:
        state = firestore_db.get_engine_state()
        processing = firestore_db.list_processing_jobs()
        return {
            "running": self._started,
            "paused": state["paused"],
            "auto_killed": state["auto_killed"],
            "workers": self.NUM_WORKERS,
            "auto_available": auto_mode_available(),
            "processing_count": len(processing),
            "processing_jobs": [
                {"job_id": j.get("id"), "uid": j.get("uid"), "title": j.get("title", "")} for j in processing
            ],
            # kept for the existing admin UI, which shows a single "current job"
            "current_job_id": processing[0].get("id") if processing else None,
            "current_uid": processing[0].get("uid") if processing else None,
        }

    # -- entry points for the Cloud Run Job worker ---------------------------
    def process_job(self, uid: str, job: dict):
        """Render one already-claimed job. Used by render_worker.py."""
        self._process(uid, job)

    def generate_auto_job(self, worker_id: str) -> bool:
        """Queue one Auto Mode job if any user is due one. Used by render_worker.py."""
        return self._auto_generate(worker_id)

    # -- worker loop ---------------------------------------------------------
    def _run(self, worker_id: str):
        auto_cooldown_until = 0.0
        while True:
            state = firestore_db.get_engine_state()
            if state["paused"]:
                # Wait on the event rather than sleeping: resume() calls wake(),
                # so this still returns instantly, but a paused engine now costs
                # ~6K Firestore reads/day per worker instead of ~86K.
                self._wake.wait(timeout=15)
                self._wake.clear()
                continue

            next_job = store.next_pending(worker_id)
            if next_job is not None:
                uid, job = next_job
                self._process(uid, job)
                continue

            # Queue is empty. Round-robin across users with auto-mode enabled.
            if not state["auto_killed"] and time.time() >= auto_cooldown_until:
                if self._auto_generate(worker_id):
                    continue
                auto_cooldown_until = time.time() + 30

            # Idle: wait until woken by a new job / resume / settings change.
            # Every new job calls wake(), so the timeout is only a safety net -
            # it does not delay job pickup, it just stops the idle loop from
            # re-reading engine state 17K times a day per worker.
            self._wake.wait(timeout=20)
            self._wake.clear()

    def _auto_generate(self, worker_id: str) -> bool:
        if not auto_mode_available():
            return False
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if _auto_quota["day"] != today:
            _auto_quota.update(day=today, count=0)
        if _auto_quota["count"] >= AUTO_DAILY_LIMIT:
            return False

        user = firestore_db.claim_next_auto_mode_user(worker_id)
        if not user:
            return False
        uid = user["uid"]
        try:
            with _user_config_scope(uid) as profile:
                job = generate_viral_job(uid, profile)
            _auto_quota["count"] += 1
            logger.success(
                f"auto-mode created job {job['id']} for {uid} - {job['title']} "
                f"({_auto_quota['count']}/{AUTO_DAILY_LIMIT} today)"
            )
            return True
        except Exception as e:  # noqa: BLE001 - keep the loop alive on any failure
            logger.error(f"auto-mode generation failed for {uid}: {e}")
            return False

    def _process(self, uid: str, job: dict):
        job_id = job["id"]

        if job.get("kind") == "clip":
            store.update(uid, job_id, progress=1, error="")
            logger.info(f"processing clip job {job_id} for {uid}")
            try:
                self._process_clip_job(uid, job)
            except Exception as e:  # noqa: BLE001 - keep the loop alive on any failure
                logger.exception(f"clip job {job_id} crashed")
                store.update(uid, job_id, status=STATUS_FAILED, error=str(e))
            return

        task_id = utils.get_uuid()
        store.update(uid, job_id, progress=1, task_id=task_id, error="")
        logger.info(f"processing job {job_id} for {uid} (task {task_id})")

        try:
            params = VideoParams(**job["params"])
        except Exception as e:
            logger.error(f"invalid params for job {job_id}: {e}")
            store.update(uid, job_id, status=STATUS_FAILED, error=f"Invalid parameters: {e}")
            return

        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=0)

        result_holder = {}

        def _work():
            try:
                with _user_config_scope(uid):
                    result_holder["result"] = tm.start(task_id=task_id, params=params, stop_at="video")
            except Exception as e:  # noqa: BLE001 - surface any pipeline error to the UI
                logger.exception(f"job {job_id} crashed")
                result_holder["error"] = str(e)

        worker = threading.Thread(target=_work, name=f"job-{job_id}", daemon=True)
        worker.start()

        # Mirror pipeline progress into the job while it runs.
        while worker.is_alive():
            t = sm.state.get_task(task_id)
            if t:
                store.update(uid, job_id, progress=max(1, int(t.get("progress", 0))))
            time.sleep(1.0)
        worker.join()

        final_state = sm.state.get_task(task_id) or {}
        pipeline_error = result_holder.get("error")
        result = result_holder.get("result")

        if pipeline_error or final_state.get("state") == const.TASK_STATE_FAILED or not result:
            msg = (
                pipeline_error
                or final_state.get("error")
                or "Generation failed. Check the server logs (API keys, network, or voice/language mismatch)."
            )
            store.update(uid, job_id, status=STATUS_FAILED, error=msg)
            logger.error(f"job {job_id} failed: {msg}")
        else:
            urls = _collect_outputs(job_id, task_id, result)
            final_script = result.get("script", "") or job["params"].get("video_script", "")
            subject = job["params"].get("video_subject", job.get("title", ""))

            # Cross-platform publishing metadata (title / description / tags).
            meta = {}
            meta_file = ""
            try:
                with _user_config_scope(uid) as profile:
                    meta = generate_publish_metadata(
                        subject, final_script, profile, video_aspect=job["params"].get("video_aspect", "9:16")
                    )
                meta_file = _write_publish_file(job_id, meta)
            except Exception as e:  # never fail a rendered video over metadata
                logger.warning(f"job {job_id}: metadata generation failed: {e}")
                meta = {"title": job.get("title", subject), "description": "", "tags": []}

            store.update(
                uid, job_id,
                status=STATUS_DONE,
                progress=100,
                videos=urls,
                script=final_script,
                meta=meta,
                meta_file=meta_file,
                error="",
            )
            logger.success(f"job {job_id} done, {len(urls)} video(s)")
            with _user_config_scope(uid):
                self._auto_publish(uid, job_id, urls, meta)

    def _process_clip_job(self, uid: str, job: dict):
        """Cut, letterbox and caption one highlight segment from an uploaded
        long-form video. Unlike _process(), this never touches task.py's
        script-to-video pipeline - the source video already has picture and
        sound, it just needs to be trimmed and reframed for Shorts."""
        job_id = job["id"]
        params = job["params"]
        source_path = params["source_path"]
        start, end = float(params["start"]), float(params["end"])
        transcript_segments = [tuple(x) for x in params.get("transcript_segments", [])]

        if not os.path.isfile(source_path):
            raise RuntimeError("uploaded source video is no longer available")

        store.update(uid, job_id, progress=10)
        out_name = f"{job_id}.mp4"
        out_path = os.path.join(output_dir(), out_name)

        with _user_config_scope(uid):
            clips.render_clip(
                source_path=source_path, start=start, end=end, out_path=out_path,
                subtitle_enabled=bool(config.ui.get("subtitle_enabled", True)),
                font_name=config.ui.get("font_name", "MicrosoftYaHeiBold.ttc"),
                font_size=int(config.ui.get("font_size", 60)),
                subtitle_position=config.ui.get("subtitle_position", "bottom"),
                text_color=config.ui.get("text_fore_color", "#FFFFFF"),
                transcript_segments=transcript_segments,
                logo_path=params.get("logo_path", ""),
                contact_website=params.get("contact_website", ""),
                contact_phone=params.get("contact_phone", ""),
            )

        if not os.path.isfile(out_path):
            raise RuntimeError("clip render produced no output file")

        store.update(uid, job_id, progress=90)
        public_url = f"/media/{out_name}"

        meta = {}
        meta_file = ""
        try:
            with _user_config_scope(uid) as profile:
                meta = generate_publish_metadata(
                    job.get("title", "Highlight clip"), params.get("transcript_excerpt", ""), profile
                )
            meta_file = _write_publish_file(job_id, meta)
        except Exception as e:  # never fail a rendered clip over metadata
            logger.warning(f"clip job {job_id}: metadata generation failed: {e}")
            meta = {"title": job.get("title", "Highlight clip"), "description": "", "tags": []}

        store.update(
            uid, job_id,
            status=STATUS_DONE, progress=100,
            videos=[public_url], meta=meta, meta_file=meta_file, error="",
        )
        logger.success(f"clip job {job_id} done")
        with _user_config_scope(uid):
            self._auto_publish(uid, job_id, [public_url], meta)

        self._cleanup_source_if_unused(uid, source_path)

    def _cleanup_source_if_unused(self, uid: str, source_path: str):
        """Delete an uploaded source video once no other queued/running clip
        job still needs it - uploads can be large and aren't shown anywhere
        once their clips exist."""
        try:
            jobs = store.all(uid)
            still_needed = any(
                j.get("kind") == "clip"
                and j.get("status") in (STATUS_PENDING, STATUS_PROCESSING)
                and j.get("params", {}).get("source_path") == source_path
                for j in jobs
            )
            if not still_needed and os.path.isfile(source_path):
                os.remove(source_path)
        except Exception as e:  # noqa: BLE001 - cleanup is best-effort
            logger.warning(f"failed to clean up clip source {source_path}: {e}")

    def _auto_publish(self, uid: str, job_id: str, urls: list, meta: dict):
        """If enabled, publish the finished video to connected platforms."""
        try:
            if not config.app.get("auto_publish") or not urls:
                return
            wanted = config.app.get("auto_publish_platforms") or []
            st = publish.status(uid)
            plats = [p for p in wanted if st.get(p, {}).get("connected")]
            if not plats:
                return
            video_path = os.path.join(output_dir(), os.path.basename(urls[0]))
            results = publish.publish_video(uid, video_path, meta, plats)
            store.update(uid, job_id, publish=results)
            ok = [p for p in plats if results.get(p, {}).get("success")]
            failed = {p: results[p].get("error") for p in plats if not results.get(p, {}).get("success")}
            if ok:
                logger.info(f"auto-published job {job_id} to {ok}")
            if failed:
                logger.warning(f"auto-publish failed for job {job_id}: {failed}")
            elif ok and DELETE_AFTER_PUBLISH:
                self._drop_published_files(uid, job_id, urls)
        except Exception as e:  # noqa: BLE001 - publishing must never fail a render
            logger.warning(f"auto-publish failed for {job_id}: {e}")

    def _drop_published_files(self, uid: str, job_id: str, urls: list):
        """Delete a job's rendered files once every requested platform has it.

        Only called when nothing failed - a partial publish keeps its file so
        the failed platform can be retried. The job keeps its publish results
        (YouTube/TikTok ids), so the video is still reachable; it just loses
        the in-dashboard player and Download button, which the UI handles by
        checking videos.length before rendering either.
        """
        removed = 0
        for u in urls:
            path = os.path.join(output_dir(), os.path.basename(u))
            try:
                os.remove(path)
                removed += 1
            except FileNotFoundError:
                pass
            except Exception as e:  # noqa: BLE001 - never fail a finished render
                logger.warning(f"could not delete {path}: {e}")
        if removed:
            store.update(uid, job_id, videos=[], storage_freed=True)
            logger.info(f"freed {removed} published file(s) for job {job_id}")


engine = Engine()
