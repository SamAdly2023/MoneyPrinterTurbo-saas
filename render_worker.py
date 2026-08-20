"""Cloud Run **Job** entrypoint: drain the render queue, then exit.

The web service used to render videos itself, in background threads, which
forced it to run with CPU always allocated (4 vCPU + 8 GiB, billed 24/7 at
roughly $10/day) so those threads kept running between requests. Almost all
of that money bought idle time: the container sat awake waiting for work.

This script is the other half of the split. It runs the exact same image and
the exact same pipeline, but as a Cloud Run Job: it starts, renders whatever
is pending, and exits. Billing stops when it exits. The web service can then
run request-only and scale to zero.

It deliberately does NOT take a job id. It claims work through the same
atomic transaction the old engine used (firestore_db.claim_next_pending_job),
so if two executions overlap - because two videos were queued a second apart
- they simply take different jobs, and an execution that finds an empty queue
exits immediately. That is cheaper and far less fragile than passing ids
around and hoping the right worker gets the right one.

Run modes:
  (default)              drain pending render jobs until the queue is empty
  MPT_WORKER_AUTO=1      run one round of auto-mode generation instead, for
                         Cloud Scheduler to invoke on whatever cadence you
                         want Auto Mode videos to appear

Env it honours (all optional):
  MPT_WORKER_MAX_JOBS     stop after this many jobs   (default 25)
  MPT_WORKER_MAX_SECONDS  stop after this long        (default 3000, under
                          Cloud Run Jobs' 3600s task timeout)
"""

import os
import sys
import time

from loguru import logger

# Match main.py: the repo root has to be importable before app.* resolves.
_root = os.path.dirname(os.path.realpath(__file__))
if _root not in sys.path:
    sys.path.append(_root)

from app.services import firestore_db, render_dispatch, saas  # noqa: E402
from app.utils import utils  # noqa: E402

MAX_JOBS = max(1, int(os.getenv("MPT_WORKER_MAX_JOBS", "25")))
MAX_SECONDS = max(60, int(os.getenv("MPT_WORKER_MAX_SECONDS", "3000")))

# How many Auto Mode videos one scheduled run may create. This has to be small,
# and it has to be enforced here: claim_next_auto_mode_user() only round-robins
# by "who waited longest" - there is no per-user daily cap anywhere in the
# codebase. With a single auto-mode user it returns that same user every call,
# so an unbounded loop would generate videos until it hit MAX_JOBS, every hour.
# Videos per day = this number x how often Cloud Scheduler fires.
AUTO_MAX = max(1, int(os.getenv("MPT_WORKER_AUTO_MAX", "1")))


def _drain(worker_id: str) -> int:
    """Claim and render pending jobs until the queue empties. Returns the count."""
    started = time.time()
    done = 0

    while done < MAX_JOBS:
        elapsed = time.time() - started
        if elapsed > MAX_SECONDS:
            # Leave the rest queued rather than being killed mid-render by the
            # task timeout - the next execution picks them straight back up.
            logger.warning(f"stopping after {elapsed:.0f}s with work still queued")
            break

        if firestore_db.get_engine_state()["paused"]:
            logger.info("engine is paused - exiting without claiming work")
            break

        claimed = saas.store.next_pending(worker_id)
        if claimed is None:
            break

        uid, job = claimed
        logger.info(f"rendering job {job['id']} for {uid}")
        saas.engine.process_job(uid, job)
        done += 1

    return done


def _auto_round(worker_id: str) -> int:
    """One round of Auto Mode generation, round-robining across opted-in users.

    With the always-on engine gone, nothing is sitting in a loop waiting to
    generate these - point Cloud Scheduler at this job with MPT_WORKER_AUTO=1
    at whatever interval you want new Auto Mode videos to appear.
    """
    state = firestore_db.get_engine_state()
    if state["paused"] or state["auto_killed"]:
        logger.info("engine paused or auto-mode killed - nothing to generate")
        return 0

    # Auto-generation queues jobs through create_job, which would normally
    # start a fresh execution per video. Render them here instead - one
    # container doing both is cheaper than N containers each doing one.
    render_dispatch.suppress()

    generated = 0
    while generated < AUTO_MAX and saas.engine.generate_auto_job(worker_id):
        generated += 1

    if generated:
        logger.info(f"generated {generated} auto job(s) - rendering them now")
        _drain(worker_id)
    return generated


def main() -> int:
    worker_id = f"job-{utils.get_uuid(remove_hyphen=True)[:8]}"
    auto = os.getenv("MPT_WORKER_AUTO", "").strip().lower() in ("1", "true", "yes")

    started = time.time()
    if auto:
        logger.info(f"auto-mode worker {worker_id} starting")
        count = _auto_round(worker_id)
        logger.success(f"queued {count} auto job(s) in {time.time() - started:.0f}s")
    else:
        logger.info(f"render worker {worker_id} starting")
        count = _drain(worker_id)
        logger.success(f"rendered {count} job(s) in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a non-zero exit is what tells Cloud Run it failed
        logger.exception("render worker crashed")
        sys.exit(1)
