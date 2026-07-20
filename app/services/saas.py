"""
SaaS layer for MoneyPrinterTurbo.

Adds a persistent, self-running "video creation engine" on top of the existing
generation pipeline (app/services/task.py):

    - A JSON-backed job store (survives restarts).
    - A single background worker that runs saved scripts one-by-one.
    - Generated videos are copied into a local output folder.

This module deliberately runs `task.start()` directly (instead of going through
the in-memory TaskManager) so the queue is strictly sequential and fully owned
by the dashboard, with clean pending -> processing -> done/failed semantics.
"""

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

_JOBS_FILE = os.path.join(utils.storage_dir("saas", create=True), "jobs.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def output_dir() -> str:
    """Folder where finished videos are collected for the user."""
    return utils.storage_dir("output", create=True)


class JobStore:
    """Thread-safe, file-backed collection of jobs."""

    def __init__(self, path: str = _JOBS_FILE):
        self._path = path
        self._lock = threading.RLock()
        self._jobs = self._load()

    def _load(self):
        if not os.path.isfile(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            logger.warning("jobs.json is not a list, starting empty")
        except Exception as e:  # corrupt file should not crash the server
            logger.warning(f"failed to load jobs.json: {e}")
        return []

    def _flush_locked(self):
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._jobs, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def all(self):
        with self._lock:
            return copy.deepcopy(self._jobs)

    def get(self, job_id: str):
        with self._lock:
            for job in self._jobs:
                if job["id"] == job_id:
                    return copy.deepcopy(job)
        return None

    def add(self, job: dict):
        with self._lock:
            self._jobs.append(job)
            self._flush_locked()
        return copy.deepcopy(job)

    def update(self, job_id: str, **changes):
        with self._lock:
            for job in self._jobs:
                if job["id"] == job_id:
                    job.update(changes)
                    job["updated_at"] = _now_iso()
                    self._flush_locked()
                    return copy.deepcopy(job)
        return None

    def delete(self, job_id: str):
        with self._lock:
            before = len(self._jobs)
            self._jobs = [j for j in self._jobs if j["id"] != job_id]
            changed = len(self._jobs) != before
            if changed:
                self._flush_locked()
            return changed

    def next_pending(self):
        with self._lock:
            for job in self._jobs:
                if job["status"] == STATUS_PENDING:
                    return copy.deepcopy(job)
        return None

    def reset_stuck_jobs(self):
        """On startup, any job left 'processing' (from a crash) goes back to the queue."""
        with self._lock:
            changed = False
            for job in self._jobs:
                if job["status"] == STATUS_PROCESSING:
                    job["status"] = STATUS_PENDING
                    job["progress"] = 0
                    job["updated_at"] = _now_iso()
                    changed = True
            if changed:
                self._flush_locked()


store = JobStore()


def create_job(title: str, params: dict, auto: bool = False) -> dict:
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
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    store.add(job)
    engine.wake()
    logger.info(f"queued job {job['id']} - {job['title']}")
    return job


# --------------------------------------------------------------------------- #
# Auto mode: let Groq invent viral YouTube Shorts and keep the queue fed.
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


def build_default_params(subject: str, script: str = "", terms: str = "") -> dict:
    """Build a validated VideoParams dict from the current defaults in config."""
    ui = config.ui
    app = config.app
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
        # Steers length when the pipeline generates the script from a subject.
        "video_script_prompt": SCRIPT_LENGTH_PROMPT,
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


def generate_viral_idea() -> dict:
    """Ask the configured LLM for one fresh viral Shorts idea + script."""
    category = random.choice(AUTO_CATEGORIES)
    prompt = (
        "You are a viral short-form video scriptwriter for YouTube Shorts, TikTok and Reels.\n"
        f"Invent ONE fresh, surprising idea about: {category}.\n\n"
        "Write the NARRATION using this proven viral structure:\n"
        "1) HOOK: open with one bold, curiosity-provoking claim or question that stops the scroll "
        "(e.g. 'Your memory is lying to you', 'Your cheap clothes are hiding a disaster').\n"
        "2) PAYOFF: 3-4 punchy, specific, surprising facts or steps that deliver on the hook.\n"
        "3) CALL TO ACTION: end with a short CTA (e.g. 'Subscribe if you remember the monocle', "
        "'Save this for your next test', 'Follow to see behind the curtain').\n"
        "The narration must be 130-170 words (about 40-80 seconds spoken), conversational and punchy, "
        "with NO emojis, NO hashtags and NO scene directions.\n\n"
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


def generate_viral_job() -> dict:
    idea = generate_viral_idea()
    params = build_default_params(idea["subject"], idea["script"], idea["keywords"])
    return create_job(title=idea["title"], params=params, auto=True)


def generate_publish_metadata(subject: str, script: str) -> dict:
    """Generate cross-platform publishing metadata (title, description, tags)."""
    prompt = (
        "You are a viral social media manager. Write publishing metadata for a short "
        "vertical video for YouTube Shorts, TikTok, Instagram Reels, Facebook, X and Pinterest.\n"
        f"Video topic: {subject}\n"
        f"Narration: {script}\n\n"
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
    """Sequential background worker that drains the pending queue."""

    def __init__(self):
        self._wake = threading.Event()
        self._paused = threading.Event()  # set == paused
        self._auto = threading.Event()    # set == auto mode on
        self._thread = None
        self._started = False
        self.current_job_id = None

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

    def auto_start(self):
        self._auto.set()
        self.wake()

    def auto_stop(self):
        self._auto.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def auto(self) -> bool:
        return self._auto.is_set()

    def status(self) -> dict:
        return {
            "running": self._started,
            "paused": self.paused,
            "auto": self.auto,
            "current_job_id": self.current_job_id,
        }

    # -- worker loop ---------------------------------------------------------
    def _run(self):
        auto_cooldown_until = 0.0
        while True:
            if self.paused:
                time.sleep(1)
                continue

            job = store.next_pending()
            if job is not None:
                self._process(job)
                continue

            # Queue is empty. In auto mode, invent a new viral video and loop.
            if self.auto and not self.paused and time.time() >= auto_cooldown_until:
                if self._auto_generate():
                    continue
                # generation failed (e.g. LLM/network) -> back off before retrying
                auto_cooldown_until = time.time() + 30
                logger.warning("auto-mode generation failed, retrying in 30s")

            # Idle: wait until woken by a new job / resume / auto toggle.
            self._wake.wait(timeout=5)
            self._wake.clear()

    def _auto_generate(self) -> bool:
        try:
            job = generate_viral_job()
            logger.success(f"auto-mode created job {job['id']} - {job['title']}")
            return True
        except Exception as e:  # noqa: BLE001 - keep the loop alive on any failure
            logger.error(f"auto-mode generation failed: {e}")
            return False

    def _process(self, job: dict):
        job_id = job["id"]
        task_id = utils.get_uuid()
        self.current_job_id = job_id
        store.update(job_id, status=STATUS_PROCESSING, progress=1, task_id=task_id, error="")
        logger.info(f"processing job {job_id} (task {task_id})")

        try:
            params = VideoParams(**job["params"])
        except Exception as e:
            logger.error(f"invalid params for job {job_id}: {e}")
            store.update(job_id, status=STATUS_FAILED, error=f"Invalid parameters: {e}")
            self.current_job_id = None
            return

        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=0)

        result_holder = {}

        def _work():
            try:
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
                store.update(job_id, progress=max(1, int(t.get("progress", 0))))
            time.sleep(1.0)
        worker.join()

        final_state = sm.state.get_task(task_id) or {}
        pipeline_error = result_holder.get("error")
        result = result_holder.get("result")

        if pipeline_error or final_state.get("state") == const.TASK_STATE_FAILED or not result:
            msg = pipeline_error or "Generation failed. Check the server logs (API keys, network, or voice/language mismatch)."
            store.update(job_id, status=STATUS_FAILED, error=msg)
            logger.error(f"job {job_id} failed: {msg}")
        else:
            urls = _collect_outputs(job_id, task_id, result)
            final_script = result.get("script", "") or job["params"].get("video_script", "")
            subject = job["params"].get("video_subject", job.get("title", ""))

            # Cross-platform publishing metadata (title / description / tags).
            meta = {}
            meta_file = ""
            try:
                meta = generate_publish_metadata(subject, final_script)
                meta_file = _write_publish_file(job_id, meta)
            except Exception as e:  # never fail a rendered video over metadata
                logger.warning(f"job {job_id}: metadata generation failed: {e}")
                meta = {"title": job.get("title", subject), "description": "", "tags": []}

            store.update(
                job_id,
                status=STATUS_DONE,
                progress=100,
                videos=urls,
                script=final_script,
                meta=meta,
                meta_file=meta_file,
                error="",
            )
            logger.success(f"job {job_id} done, {len(urls)} video(s)")
            self._auto_publish(job_id, urls, meta)

        self.current_job_id = None

    def _auto_publish(self, job_id: str, urls: list, meta: dict):
        """If enabled, publish the finished video to connected platforms."""
        try:
            if not config.app.get("auto_publish") or not urls:
                return
            wanted = config.app.get("auto_publish_platforms") or []
            st = publish.status()
            plats = [p for p in wanted if st.get(p, {}).get("connected")]
            if not plats:
                return
            video_path = os.path.join(output_dir(), os.path.basename(urls[0]))
            results = publish.publish_video(video_path, meta, plats)
            store.update(job_id, publish=results)
            logger.info(f"auto-published job {job_id} to {plats}")
        except Exception as e:  # noqa: BLE001 - publishing must never fail a render
            logger.warning(f"auto-publish failed for {job_id}: {e}")


engine = Engine()
