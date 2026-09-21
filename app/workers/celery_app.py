"""
Application Celery (section 36 : workers/celery_app.py). Broker et backend Redis,
déjà présent dans l'infrastructure depuis le Sprint 1 mais jamais utilisé jusqu'ici.
"""
from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery("bob", broker=settings.redis_url, backend=settings.redis_url)

celery_app.conf.beat_schedule = {
    "check-followups-every-15-minutes": {
        "task": "app.workers.followups.check_followups_task",
        "schedule": crontab(minute="*/15"),
    },
}
celery_app.conf.timezone = "UTC"

celery_app.autodiscover_tasks(["app.workers"])
