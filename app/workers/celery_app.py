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
    "app.workers.renewals",
    "app.workers.reports",
    "app.workers.prospects",
    "app.workers.prospect_emails",
    "app.workers.order_reminders",
    "app.workers.monitoring",
    "app.workers.wa_templates",
    "app.workers.plan_quota",
    "app.workers.coexistence_sync",
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
    # Lot 78 — boutiques Shopify : prix, stock, variantes et fiches, chaque nuit.
    "sync-shopify-nightly": {
        "task": "app.workers.catalog_sync.sync_shopify_task",
        "schedule": crontab(hour=3, minute=20),
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
    # Lot 55 — échéances des contrats du courtier : récapitulatif au cabinet et rappel unique au client.
    "contract-renewals": {
        "task": "app.workers.renewals.check_renewals_task",
        "schedule": crontab(minute=35),
    },
    # Lot 60 — rapport mensuel de début de mois (le rapport de renouvellement part avec billing-notices).
    "monthly-reports": {
        "task": "app.workers.reports.check_monthly_reports_task",
        "schedule": crontab(minute=50),
    },
    # Lot 61 — prospects restés sans réaction depuis un an : effacés (données personnelles).
    "purge-prospects": {
        "task": "app.workers.prospects.purge_prospects_task",
        "schedule": crontab(hour=3, minute=40),
    },
    # Lot 62 — emails de prospection : un email toutes les 3 minutes au plus (plafond et heures d'envoi vérifiés).
    "prospect-emails": {
        "task": "app.workers.prospect_emails.send_prospect_email_task",
        "schedule": crontab(minute="*/3"),
    },
    # Lot 62 — boîte de prospection lue toutes les 15 minutes (réponses, adresses inexistantes).
    "prospect-inbox": {
        "task": "app.workers.prospect_emails.read_prospect_inbox_task",
        "schedule": crontab(minute="*/15"),
    },
    # Lot 63 — commandes non payées : relance 3 h après, puis le lendemain (entre 9 h et 20 h, heure du pays).
    "order-reminders": {
        "task": "app.workers.order_reminders.check_order_reminders_task",
        "schedule": crontab(minute=10),
    },
    # Lot 67 — monitoring : erreurs rangées et alertes chaque minute ; effacement après 30 jours chaque nuit.
    "monitoring-drain": {
        "task": "app.workers.monitoring.drain_task",
        "schedule": crontab(minute="*"),
    },
    # Lot 68 — statut chez Meta des modèles WhatsApp encore en attente (rien si l'interrupteur est éteint).
    "wa-templates-refresh": {
        "task": "app.workers.wa_templates.refresh_templates_task",
        "schedule": crontab(minute=25),
    },
    # Lot 69 — plafonds des formules : alertes à 80 % et 100 % (Bob ne s'arrête pas, sauf mode « pause »).
    "plan-quota-alerts": {
        "task": "app.workers.plan_quota.check_quotas_task",
        "schedule": crontab(minute=40),
    },
    "monitoring-purge": {
        "task": "app.workers.monitoring.purge_task",
        "schedule": crontab(hour=4, minute=10),
    },
    # Lot 52 — coexistence : contacts et historique du téléphone, redemandés à Meta pendant les 24 h.
    "coexistence-sync-retry": {
        "task": "app.workers.coexistence_sync.retry_syncs_task",
        "schedule": crontab(minute="*/15"),
    },
}
celery_app.conf.timezone = "UTC"


# Lot 67 — monitoring : journal de l'application branché, tâches en échec capturées, dernier passage de chaque tâche.
def _install_monitoring() -> None:
    from datetime import datetime, timezone

    from celery.signals import task_failure, task_postrun, task_prerun

    from app.services import monitoring

    monitoring.install()
    started: dict = {}

    @task_prerun.connect(weak=False)
    def _on_prerun(task_id=None, task=None, **kwargs):
        started[task_id] = datetime.now(timezone.utc)

    @task_postrun.connect(weak=False)
    def _on_postrun(task_id=None, task=None, state=None, **kwargs):
        monitoring.record_task_run(getattr(task, "name", "?"), state or "?", started.pop(task_id, None))

    @task_failure.connect(weak=False)
    def _on_failure(sender=None, exception=None, einfo=None, **kwargs):
        monitoring.capture_task_failure(getattr(sender, "name", "?"), exception, getattr(einfo, "tb", None))


_install_monitoring()

