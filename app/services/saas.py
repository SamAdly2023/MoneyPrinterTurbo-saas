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
from app.models.schema import VideoParams
from app.services import firestore_db
from app.services import llm
from app.services import publish
from app.services import state as sm
from app.services import task as tm
from app.utils import utils

# Job lifecycle statuses used by the dashboard.
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# Admin-managed, shared by every user's jobs (app_config/global in Firestore).
APP_STR_KEYS = {
    "video_source", "extra_token", "llm_provider",
    "groq_api_key", "groq_model_name", "grok_api_key", "grok_model_name",
    "openai_api_key", "openai_base_url", "openai_model_name",
    "youtube_client_id", "youtube_client_secret",
    "tiktok_client_key", "tiktok_client_secret", "publish_base_url",
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

    def next_pending(self):
        """Oldest pending job across every user: (uid, job) or None."""
        return firestore_db.next_pending_job()

    def reset_stuck_jobs(self):
        firestore_db.reset_stuck_jobs()


store = JobStore()


def create_job(uid: str, title: str, params: dict, auto: bool = False) -> dict:
    job = {
        "id": utils.get_uuid(),
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
    engine.wake()
    logger.info(f"queued job {job['id']} for {uid} - {job['title']}")
    return job


# --------------------------------------------------------------------------- #
# Per-user config scoping
# --------------------------------------------------------------------------- #
# Held for the *entire* duration of any scoped block below, whether that's
# the engine rendering a job or an API request doing a one-off LLM call
# (e.g. generate-script). Without this, a request-time scope and the
# engine's job-time scope could interleave and clobber each other's overlay
# on the shared config.app/config.ui dicts. This does mean an on-demand
# call briefly queues behind a job the engine is currently processing -
# acceptable for this scale, and consistent with the engine itself being a
# single sequential worker.
_config_scope_lock = threading.RLock()


@contextlib.contextmanager
def _user_config_scope(uid: str):
    """Overlay the shared global settings + this job owner's business profile
    onto the shared config globals for the duration of the block.

    Every scoped key is reset to a blank baseline first (not just merged),
    so a previous job can never leak a value into the next one. Yields the
    owner's profile dict (business name/site/bio etc.) for prompt injection.
    """
    _config_scope_lock.acquire()
    global_settings = firestore_db.get_global_settings()
    profile = firestore_db.get_user_profile(uid)

    snapshot_app = {k: config.app.get(k) for k in APP_STR_KEYS | APP_LIST_KEYS | PROFILE_APP_KEYS | APP_OTHER_KEYS}
    snapshot_ui = {k: config.ui.get(k) for k in UI_KEYS}

    try:
        for k in APP_STR_KEYS:
            config.app[k] = global_settings.get(k, "")
        for k in APP_LIST_KEYS:
            v = global_settings.get(k) or []
            config.app[k] = [v] if isinstance(v, str) and v else (v if isinstance(v, list) else [])
        for k in PROFILE_APP_KEYS:
            config.app[k] = profile.get(k, "")
        for k in APP_OTHER_KEYS:
            config.app[k] = profile.get(k, False if k == "auto_publish" else [])
        for k in UI_KEYS:
            if k in global_settings:
                config.ui[k] = global_settings[k]
        yield profile
    finally:
        for k, v in snapshot_app.items():
            if v is None:
                config.app.pop(k, None)
            else:
                config.app[k] = v
        for k, v in snapshot_ui.items():
            if v is None:
                config.ui.pop(k, None)
            else:
                config.ui[k] = v
        _config_scope_lock.release()


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


def build_default_params(subject: str, script: str = "", terms: str = "", profile: dict = None) -> dict:
    """Build a validated VideoParams dict from the current defaults in config."""
    ui = config.ui
    app = config.app
    script_prompt = SCRIPT_LENGTH_PROMPT
    business_context = _business_context_prompt(profile) if profile else ""
    if business_context:
        script_prompt = script_prompt + " " + business_context
    raw = {
        "video_subject": (subject or "").strip(),
        "video_script": (script or "").strip(),
        "video_terms": (terms or "").strip() or None,
        "video_source": app.get("video_source", "pexels"),
        "video_aspect": ui.get("video_aspect", "9:16"),
        "voice_name": ui.get("voice_name", "en-US-AndrewNeural-Male"),
        "subtitle_enabled": ui.get("subtitle_enabled", True),
        "video_clip_duration": ui.get("video_clip_duration", 5),
        "paragraph_number": ui.get("paragraph_number", DEFAULT_PARAGRAPHS),
        "video_count": 1,
        "bgm_type": ui.get("bgm_type", "random"),
        "font_size": ui.get("font_size", 60),
        "subtitle_position": ui.get("subtitle_position", "bottom"),
        "font_name": ui.get("font_name", "MicrosoftYaHeiBold.ttc"),
        "text_fore_color": ui.get("text_fore_color", "#FFFFFF"),
        # Steers length (and, if set, this business's branding) when the
        # pipeline generates the script from a subject.
        "video_script_prompt": script_prompt,
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
    business_context = _business_context_prompt(profile)
    prompt = (
        "You are a viral short-form video scriptwriter for YouTube Shorts, TikTok and Reels.\n"
        + topic_line +
        "Write the NARRATION using this proven viral structure:\n"
        "1) HOOK: open with one bold, curiosity-provoking claim or question that stops the scroll "
        "(e.g. 'Your memory is lying to you', 'Your cheap clothes are hiding a disaster').\n"
        "2) PAYOFF: 3-4 punchy, specific, surprising facts or steps that deliver on the hook.\n"
        "3) CALL TO ACTION: end with a short CTA (e.g. 'Subscribe if you remember the monocle', "
        "'Save this for your next test', 'Follow to see behind the curtain').\n"
        "The narration must be 130-170 words (about 40-80 seconds spoken), conversational and punchy, "
        "with NO emojis, NO hashtags and NO scene directions.\n\n"
        + (business_context + "\n\n" if business_context else "") +
        "Return ONLY valid minified JSON (no markdown, no commentary) with exactly these keys:\n"
        '{"title": "short clean topic label, 3-6 words, no emojis", '
        '"subject": "the core topic in a few words", '
        '"script": "the narration following the HOOK / PAYOFF / CALL TO ACTION structure above", '
        '"keywords": "5-6 comma-separated CONCRETE visual scenes to find stock footage, '
        'e.g. \\"monopoly board, darth vader mask, confused crowd, mirror reflection\\""}\n'
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


def generate_viral_job(uid: str, profile: dict = None) -> dict:
    idea = generate_viral_idea(profile)
    params = build_default_params(idea["subject"], idea["script"], idea["keywords"], profile=profile)
    return create_job(uid, title=idea["title"], params=params, auto=True)


def generate_publish_metadata(subject: str, script: str, profile: dict = None) -> dict:
    """Generate cross-platform publishing metadata (title, description, tags)."""
    business_context = _business_context_prompt(profile) if profile else ""
    prompt = (
        "You are a viral social media manager. Write publishing metadata for a short "
        "vertical video for YouTube Shorts, TikTok, Instagram Reels, Facebook, X and Pinterest.\n"
        f"Video topic: {subject}\n"
        f"Narration: {script}\n\n"
        + (business_context + "\n\n" if business_context else "") +
        "Match this proven viral style exactly:\n"
        "- title: catchy and curiosity-driven, under 90 characters, ending with 1-2 relevant emojis "
        "(e.g. 'The Mandela Effect Proves Reality is Broken \U0001F633\U0001F300').\n"
        "- description: 1-2 short punchy sentences that hook the viewer, then a space, then 5-7 hashtags "
        "that start with #shorts (e.g. '...why do we all remember it wrong? #shorts #mandelaeffect #mindblown').\n"
        "- tags: 7-10 SEO keyword phrases that can be multiple words, lowercase, WITHOUT the # symbol "
        "(e.g. 'mandela effect', 'glitch in the matrix', 'false memories').\n\n"
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
    """Sequential background worker that drains the pending queue for everyone."""

    def __init__(self):
        self._wake = threading.Event()
        self._paused = threading.Event()  # set == paused
        self._auto_kill = threading.Event()  # set == admin globally disabled auto-mode
        self._thread = None
        self._started = False
        self.current_job_id = None
        self.current_uid = None

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        if self._started:
            return
        self._started = True
        store.reset_stuck_jobs()
        self._thread = threading.Thread(target=self._run, name="saas-engine", daemon=True)
        self._thread.start()
        logger.success("video creation engine started")

    def wake(self):
        self._wake.set()

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()
        self.wake()

    def auto_kill_start(self):
        """Admin-only global kill switch: stop ALL users' auto-mode generation."""
        self._auto_kill.set()

    def auto_kill_stop(self):
        self._auto_kill.clear()
        self.wake()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def auto_killed(self) -> bool:
        return self._auto_kill.is_set()

    def status(self) -> dict:
        return {
            "running": self._started,
            "paused": self.paused,
            "auto_killed": self.auto_killed,
            "current_job_id": self.current_job_id,
            "current_uid": self.current_uid,
        }

    # -- worker loop ---------------------------------------------------------
    def _run(self):
        auto_cooldown_until = 0.0
        while True:
            if self.paused:
                time.sleep(1)
                continue

            next_job = store.next_pending()
            if next_job is not None:
                uid, job = next_job
                self._process(uid, job)
                continue

            # Queue is empty. Round-robin across users with auto-mode enabled.
            if not self.auto_killed and time.time() >= auto_cooldown_until:
                if self._auto_generate():
                    continue
                auto_cooldown_until = time.time() + 30

            # Idle: wait until woken by a new job / resume / settings change.
            self._wake.wait(timeout=5)
            self._wake.clear()

    def _auto_generate(self) -> bool:
        user = firestore_db.next_auto_mode_user()
        if not user:
            return False
        uid = user["uid"]
        try:
            with _user_config_scope(uid) as profile:
                job = generate_viral_job(uid, profile)
            firestore_db.mark_auto_generated(uid)
            logger.success(f"auto-mode created job {job['id']} for {uid} - {job['title']}")
            return True
        except Exception as e:  # noqa: BLE001 - keep the loop alive on any failure
            firestore_db.mark_auto_generated(uid)  # still rotate past this user
            logger.error(f"auto-mode generation failed for {uid}: {e}")
            return False

    def _process(self, uid: str, job: dict):
        job_id = job["id"]
        task_id = utils.get_uuid()
        self.current_job_id = job_id
        self.current_uid = uid
        store.update(uid, job_id, status=STATUS_PROCESSING, progress=1, task_id=task_id, error="")
        logger.info(f"processing job {job_id} for {uid} (task {task_id})")

        try:
            params = VideoParams(**job["params"])
        except Exception as e:
            logger.error(f"invalid params for job {job_id}: {e}")
            store.update(uid, job_id, status=STATUS_FAILED, error=f"Invalid parameters: {e}")
            self.current_job_id = None
            self.current_uid = None
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
            msg = pipeline_error or "Generation failed. Check the server logs (API keys, network, or voice/language mismatch)."
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
                    meta = generate_publish_metadata(subject, final_script, profile)
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

        self.current_job_id = None
        self.current_uid = None

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
            logger.info(f"auto-published job {job_id} to {plats}")
        except Exception as e:  # noqa: BLE001 - publishing must never fail a render
            logger.warning(f"auto-publish failed for {job_id}: {e}")


engine = Engine()
