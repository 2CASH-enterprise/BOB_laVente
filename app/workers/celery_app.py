"""
Application Celery (section 36 : workers/celery_app.py). Broker et backend Redis,
déjà présent dans l'infrastructure depuis le Sprint 1 mais jamais utilisé jusqu'ici.
"""
from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

# Modules de tâches déclarés EXPLICITEMENT : autodiscover_tasks(["app.workers"]) cherche un
# fichier app/workers/tasks.py qui n'existe pas, et ne trouvait donc AUCUNE tâche — le worker
# refusait silencieusement les relances et le calcul des opportunités (« unregistered task »).
#
# Les relances automatiques (app.workers.followups) ne sont VOLONTAIREMENT PAS activées : elles
# n'ont en réalité jamais tourné, et les activer en l'état enverrait des messages libres hors de
# la fenêtre de 24 h de WhatsApp (refusés par Meta, mais enregistrés comme « envoyés »), sans
# limite d'ancienneté des conversations. À réactiver après refonte (modèles Meta, garde-fous).
TASK_MODULES = [
    "app.workers.opportunities",
]

celery_app = Celery("bob", broker=settings.redis_url, backend=settings.redis_url, include=TASK_MODULES)

celery_app.conf.beat_schedule = {
    # Phase 0 — mesure des issues : recalcul complet chaque nuit, à une heure creuse.
    "recompute-sales-opportunities-nightly": {
        "task": "app.workers.opportunities.recompute_opportunities_task",
        "schedule": crontab(hour=2, minute=30),
    },
}
celery_app.conf.timezone = "UTC"

