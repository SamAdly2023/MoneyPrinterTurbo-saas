"""Initializes the Firebase Admin SDK once, shared by auth + Firestore access.

On Cloud Run this picks up Application Default Credentials automatically
(the runtime service account). For local dev, run
`gcloud auth application-default login` first, or set
GOOGLE_APPLICATION_CREDENTIALS to a service account key file.
"""

import firebase_admin

if not firebase_admin._apps:
    firebase_admin.initialize_app()
