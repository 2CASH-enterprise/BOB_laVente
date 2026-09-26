"""
Garde-fou : chaque tâche planifiée doit être réellement enregistrée par le worker.
Sans ce test, un worker qui refuse silencieusement une tâche (« unregistered task »)
passe inaperçu — c'est ce qui était arrivé aux relances automatiques.
"""
from app.workers.celery_app import celery_app


def _registered_tasks() -> set[str]:
    celery_app.loader.import_default_modules()  # ce que fait le worker au démarrage
    return {name for name in celery_app.tasks if name.startswith("app.")}


def test_every_scheduled_task_is_registered():
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert scheduled, "aucune tâche planifiée"
    assert scheduled <= _registered_tasks()


def test_nightly_opportunity_task_is_scheduled_and_registered():
    assert "app.workers.opportunities.recompute_opportunities_task" in _registered_tasks()
    schedules = {entry["task"]: entry["schedule"] for entry in celery_app.conf.beat_schedule.values()}
    nightly = schedules["app.workers.opportunities.recompute_opportunities_task"]
    assert nightly.hour == {2} and nightly.minute == {30}


def test_followups_stay_disabled_until_redesigned():
    """Relances désactivées volontairement (fenêtre 24 h WhatsApp, absence de limite d'ancienneté)."""
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert "app.workers.followups.check_followups_task" not in scheduled
