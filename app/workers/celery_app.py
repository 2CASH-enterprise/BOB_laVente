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
# Lot 50 — les relances sont réactivées : depuis le lot 49 elles partent par EMAIL (jamais sur
# WhatsApp), seulement aux clients qui ont accepté les offres, et seulement pour des conversations
# de moins de 14 jours (followup_service.FOLLOWUP_MAX_AGE).
TASK_MODULES = [
    "app.workers.opportunities",
    "app.workers.catalog_sync",
    "app.workers.appointment_reminders",
    "app.workers.notifications",
    "app.workers.followups",
    "app.workers.llm_budget",
    "app.workers.billing",
]

celery_app = Celery("bob", broker=settings.redis_url, backend=settings.redis_url, include=TASK_MODULES)

celery_app.conf.beat_schedule = {
    # Phase 0 — mesure des issues : recalcul complet chaque nuit, à une heure creuse.
    "recompute-sales-opportunities-nightly": {
        "task": "app.workers.opportunities.recompute_opportunities_task",
        "schedule": crontab(hour=2, minute=30),
    },
    # Lot 21 — catalogues Meta connectés : prix, stock et nouveaux produits, chaque nuit.
    "sync-meta-catalogs-nightly": {
        "task": "app.workers.catalog_sync.sync_meta_catalogs_task",
        "schedule": crontab(hour=3, minute=0),
    },
    # Lot 25 — rappels de rendez-vous : la veille à partir de 18 h, heure de chaque boutique.
    "appointment-reminders": {
        "task": "app.workers.appointment_reminders.send_appointment_reminders_task",
        "schedule": crontab(minute="*/15"),
    },
    # Lot 37b — pastille et notifications : ce qui apparaît avec le temps (rendez-vous passé…).
    "notifications-check": {
        "task": "app.workers.notifications.check_all_task",
        "schedule": crontab(minute="*/10"),
    },
    # Lot 49/50 — relances par email des conversations restées sans réponse (boutiques qui les ont activées).
    "email-followups": {
        "task": "app.workers.followups.check_followups_task",
        "schedule": crontab(minute="*/15"),
    },
    # Lot 50 — alerte au Super Admin si le coût IA d'une boutique dépasse le seuil du mois.
    "llm-budget-alerts": {
        "task": "app.workers.llm_budget.check_budgets_task",
        "schedule": crontab(minute=5),
    },
    # Lot 51 — échéance de l'abonnement : emails au commerçant (7 jours avant, jour de grâce, pause).
    "billing-notices": {
        "task": "app.workers.billing.check_billing_task",
        "schedule": crontab(minute=20),
    },
}
celery_app.conf.timezone = "UTC"

