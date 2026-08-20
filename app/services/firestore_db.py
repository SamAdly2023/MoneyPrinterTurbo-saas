"""Data-layer facade: picks Firestore or SQLite, keeps one import for callers.

Local and hosted used to share one Firestore, which meant a video rendered on
a laptop turned up in the live dashboard pointing at a file only that laptop
had, and jobs queued by real users got claimed by whichever machine polled
first. The fix is not to change 96 call sites - it is to let this module
decide which implementation those calls land in:

    MPT_DB=sqlite     -> app/services/db_sqlite.py    (the local app)
    unset / firestore -> app/services/db_firestore.py (the hosted site)

Firebase Auth is untouched either way. Sign-in still verifies a Firebase ID
token (app/services/auth.py, which initialises firebase_admin itself), so the
local app has real Google login without a real Google database behind it.

The default is Firestore deliberately: an unset variable must never silently
move the hosted site's data onto a local file.

Nothing is imported until the choice is made - importing db_firestore calls
firestore.client() at module scope, which would demand cloud credentials on a
machine that has no business needing them.
"""

import importlib
import os

from loguru import logger

_BACKEND = (os.getenv("MPT_DB", "") or "firestore").strip().lower()

if _BACKEND in ("sqlite", "local", "sqlite3"):
    _impl = importlib.import_module("app.services.db_sqlite")
    logger.info("data backend: sqlite (local)")
else:
    _impl = importlib.import_module("app.services.db_firestore")


def backend_name() -> str:
    return "sqlite" if _impl.__name__.endswith("db_sqlite") else "firestore"


def __getattr__(name):
    """Forward every lookup to the chosen backend.

    PEP 562 module __getattr__, so `firestore_db.list_jobs(...)` and
    `firestore_db.DEFAULT_PROFILE` both resolve without re-exporting all 34
    names by hand - and a name missing from one backend raises AttributeError
    naming that backend, instead of silently resolving to something stale.
    """
    return getattr(_impl, name)


def __dir__():
    return sorted(set(dir(_impl)) | {"backend_name"})
