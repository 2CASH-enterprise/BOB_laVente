"""Lot 87 — retouche des photos en arrière-plan : fond blanc studio (chaque minute, 3 photos au plus) et photos
portées terminées chez FASHN (chaque minute)."""
import asyncio

from app.workers.celery_app import celery_app
from app.workers.inbound_leads import _with_db


LOCK_FILE = "/tmp/bob-photo-retouch.lock"


@celery_app.task(name="app.workers.photo_studio.retouch_photos_task")
def retouch_photos_task() -> dict:
    """Une seule retouche à la fois sur le serveur (le détourage prend ~0,8 Go de mémoire) : si la précédente tourne
    encore, ce passage est sauté."""
    import fcntl

    from app.services.photo_studio import run_pending

    with open(LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"skipped": "retouche déjà en cours"}
        return asyncio.run(_with_db(lambda db: run_pending(db, limit=3)))


@celery_app.task(name="app.workers.photo_studio.collect_worn_task")
def collect_worn_task() -> dict:
    from app.core.config import get_settings
    from app.services.photo_studio import collect_worn

    if not get_settings().fashn_api_key:
        return {"skipped": "FASHN non configuré"}
    return asyncio.run(_with_db(collect_worn))
