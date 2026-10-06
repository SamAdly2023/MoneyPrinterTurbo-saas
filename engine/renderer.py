"""Runs one job through the same render pipeline the server uses
(app/services/task.py), but on this computer."""

import os
import shutil
import threading
import time


def setup_environment(data_dir: str) -> None:
    """Must run before anything under app/ is imported: the pipeline reads
    these paths when its modules load."""
    os.makedirs(data_dir, exist_ok=True)
    os.environ.setdefault("MPT_STORAGE_DIR", os.path.join(data_dir, "storage"))
    os.environ.setdefault("MPT_CONFIG_FILE", os.path.join(data_dir, "config.toml"))
    # A server on shared hosting needs libx264 pinned to one thread; your own
    # PC has no such limit, so let ffmpeg use every core.
    os.environ.setdefault("MPT_ENCODE_AUTO_THREADS", "1")
    # The Engine never talks to a database or Firebase.
    os.environ.setdefault("MPT_DB", "sqlite")


class Renderer:
    def __init__(self, on_progress):
        # Imported late on purpose - see setup_environment().
        from app.config import config
        from app.models import const
        from app.models.schema import VideoParams
        from app.services import state as sm
        from app.services import task as tm
        from app.utils import utils

        self.config, self.const, self.VideoParams = config, const, VideoParams
        self.sm, self.tm, self.utils = sm, tm, utils
        self.on_progress = on_progress

    def render(self, job: dict) -> tuple[str, str]:
        """Render one job. Returns (path to the finished MP4, final script).
        Raises with a readable message if anything goes wrong."""
        params = self.VideoParams(**job["params"])
        settings = job.get("settings") or {}
        task_id = self.utils.get_uuid()
        self.sm.state.update_task(task_id, state=self.const.TASK_STATE_PROCESSING, progress=0)

        outcome = {}

        def work():
            # The pipeline reads its settings from a per-THREAD overlay, so it
            # has to be applied on the thread that does the rendering.
            self.config.app.set_overlay({
                "video_source": settings.get("video_source", "pexels"),
                "pexels_api_keys": settings.get("pexels_api_keys") or [],
                "pixabay_api_keys": settings.get("pixabay_api_keys") or [],
            })
            self.config.ui.set_overlay(settings.get("ui") or {})
            try:
                outcome["result"] = self.tm.start(task_id=task_id, params=params, stop_at="video")
            except Exception as e:  # noqa: BLE001
                outcome["error"] = f"{type(e).__name__}: {e}"
            finally:
                self.config.app.clear_overlay()
                self.config.ui.clear_overlay()

        thread = threading.Thread(target=work, name=f"render-{job['id']}", daemon=True)
        thread.start()
        last = -1
        while thread.is_alive():
            pct = int((self.sm.state.get_task(task_id) or {}).get("progress", 0))
            if pct != last:
                last = pct
                self.on_progress(pct)
            time.sleep(3)
        thread.join()

        state = self.sm.state.get_task(task_id) or {}
        result = outcome.get("result")
        videos = (result or {}).get("videos") or []
        if outcome.get("error") or not videos or state.get("state") == self.const.TASK_STATE_FAILED:
            raise RuntimeError(outcome.get("error") or state.get("error") or "the render produced no video")
        script = result.get("script") or job["params"].get("video_script", "")
        return videos[0], script

    def cleanup(self, video_path: str) -> None:
        """Drop the task's working files (the cached stock footage is kept so
        the next video can reuse it)."""
        try:
            task_dir = os.path.dirname(video_path)
            if os.path.basename(os.path.dirname(task_dir)) == "tasks":
                shutil.rmtree(task_dir, ignore_errors=True)
            elif os.path.isfile(video_path):
                os.remove(video_path)
        except OSError:
            pass
