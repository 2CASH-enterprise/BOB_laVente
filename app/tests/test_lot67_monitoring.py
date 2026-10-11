"""
Lot 67 — monitoring technique : capture (journal, API, tâches, navigateur), masquage des secrets, regroupement par
signature avec statuts et réouverture, emails (nouvelle erreur, pic, jamais un par occurrence), purge à 30 jours,
état du service, pages du Super Admin.
"""
import logging
import re
import smtplib
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.security import CurrentUser, create_access_token, get_current_user
from app.main import app
from app.models.monitoring import MonitorIssue, MonitorOccurrence
from app.models.superadmin_user import SuperAdminUser
from app.models.tenant import Tenant, TenantPlan
from app.models.whatsapp_account import WhatsAppAccount
from app.services import monitoring, monitoring_status
from app.tests.test_lot61_prospection import _admin

SA_HTML = Path("app/static/superadmin/index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_queue():
    monitoring._local_queue.clear()
    monitoring._local_marks.clear()
    monitoring._local_tasks.clear()
    yield
    monitoring._local_queue.clear()


def _queued() -> list[dict]:
    return list(monitoring._local_queue)


async def _tenant(db, name="Boutique Awa"):
    tenant = Tenant(name=name, country="CI", currency="XOF", email=f"{uuid.uuid4().hex[:6]}@l67.ci", plan=TenantPlan.PRO)
    db.add(tenant)
    await db.commit()
    return tenant


def _event(title="Relance impossible", source="TASK", location="app/workers/x.py:10 (run)", at=NOW, **kw):
    ev = monitoring.capture(source, title=title, message=kw.pop("message", title), location=location, at=at, **kw)
    monitoring._local_queue.clear()
    return ev


# ---------------------------------------------------------------- masquage

@pytest.mark.parametrize("raw, hidden, kept", [
    ("token=EAAGabc123def456ghi789jkl012mno", "EAAGabc123", "token="),
    ("Authorization: Bearer EAAG1234567890abcdefghijk", "1234567890", "Authorization"),
    ('{"access_token": "abc123xyz"}', "abc123xyz", "access_token"),
    ("PROSPECT_SMTP_PASSWORD=abcd efgh ijkl mnop", "efgh", "PROSPECT_SMTP_PASSWORD"),
    ("password=hunter2 ; suite", "hunter2", "suite"),
    ("Échec de l'envoi d'email à jean.dupont@gmail.com", "jean.dupont", "@gmail.com"),
    ("numéro +225 07 12 34 56 78", "07 12 34", "78"),
    ("clé sk-proj-AbCdEf1234567890GhIjKl", "AbCdEf1234567890", "clé"),
])
def test_mask_hides_secrets(raw, hidden, kept):
    masked = monitoring.mask(raw)
    assert hidden not in masked and kept in masked and "•••" in masked


def test_mask_bearer_without_key_name():
    assert monitoring.mask("en-tête envoyé : Bearer abc.def-12") == "en-tête envoyé : Bearer ••••••••"


def test_mask_keeps_ids_dates_and_code_paths():
    text = 'boutique 3f2a1b4c-1111-2222-3333-444455556666 le 2026-10-11 · File "/opt/bob/app/services/order_reminders.py", line 227'
    assert monitoring.mask(text) == text


def test_signature_groups_variants_but_not_places():
    a = monitoring.signature("SEND_EMAIL", None, "app/services/email_service.py:126 (send_email)", None,
                             monitoring.mask("Échec de l'envoi d'email à awa@ci.com"))
    b = monitoring.signature("SEND_EMAIL", None, "app/services/email_service.py:131 (send_email)", None,
                             monitoring.mask("Échec de l'envoi d'email à koffi@yahoo.fr"))
    c = monitoring.signature("SEND_EMAIL", None, "app/services/other.py:126 (send_email)", None,
                             monitoring.mask("Échec de l'envoi d'email à awa@ci.com"))
    d = monitoring.signature("TASK", None, "app/services/email_service.py:126 (send_email)", None,
                             monitoring.mask("Échec de l'envoi d'email à awa@ci.com"))
    assert a == b  # autre destinataire, ligne déplacée : même erreur
    assert a != c and a != d


# ---------------------------------------------------------------- capture du journal

def test_log_error_is_captured_masked_with_location():
    log = logging.getLogger("app.workers.fake_task")
    try:
        raise RuntimeError("connexion refusée password=hunter2")
    except RuntimeError:
        log.exception("Relances impossibles (boutique %s)", "3f2a1b4c-1111-2222-3333-444455556666")
    [ev] = _queued()
    assert ev["source"] == "TASK" and ev["exc_type"] == "RuntimeError"
    assert "hunter2" not in ev["trace"] and "hunter2" not in ev["message"]
    assert ev["location"].startswith("app/tests/test_lot67_monitoring.py:")
    assert ev["refs"] == ["3f2a1b4c-1111-2222-3333-444455556666"]
    assert ev["title"] == "Relances impossibles (boutique 3f2a1b4c-1111-2222-3333-444455556666)"


def test_log_filters():
    logging.getLogger("app.services.followup_service").warning("Réponse de Bob remplacée (AMOUNT)")  # pas un échec
    logging.getLogger("app.agents.orchestrator").warning("Erreur passagère du service d'IA (%s), nouvel essai dans %ss", "X", 1)
    logging.getLogger("app.services.email_service").warning("[SMTP non configuré] Email non envoyé à %s — corps: %s", "a@b.ci", "code 123456")
    try:
        raise TimeoutError("lent")
    except TimeoutError:  # erreur passagère : un nouvel essai suit, seul l'échec final compte
        logging.getLogger("app.agents.orchestrator").warning("Délai dépassé (%s), nouvel essai dans %ss", "X", 1, exc_info=True)
    logging.getLogger("other.lib").error("bruit d'une bibliothèque")
    logging.getLogger("app.services.monitoring").error("jamais soi-même")
    assert _queued() == []
    logging.getLogger("app.services.billing").warning("Email d'échéance impossible (boutique %s)", "x")
    logging.getLogger("app.services.prospect_mailer").warning("Prospection : %s", "Connexion à Gmail refusée")
    assert [e["source"] for e in _queued()] == ["SEND_EMAIL", "SEND_EMAIL"]


def test_whatsapp_refusal_once_with_meta_code_and_caller():
    from app.integrations.whatsapp.client import WhatsAppSendError

    def webhook_like():
        logging.getLogger("app.integrations.whatsapp.client").warning(
            "Meta a refusé l'envoi WhatsApp (numéro %s, HTTP %s) : %s", "109876543210987", 401,
            "code 190, Error validating access token: Session has expired; token=EAAGabcdef1234567890xyzXYZ")
        try:
            raise WhatsAppSendError(401, "code 190")
        except WhatsAppSendError:
            logging.getLogger("app.api.webhooks.whatsapp").exception("Échec de l'envoi WhatsApp réel pour la conversation %s", 1)
    webhook_like()
    [ev] = _queued()  # l'exception relancée dans le webhook n'est pas comptée deux fois
    assert ev["source"] == "SEND_WHATSAPP" and ev["meta_code"] == "190"
    assert ev["title"] == "Meta refuse le message : jeton d'accès expiré ou invalide (code 190)"
    assert "EAAGabcdef1234567890" not in ev["message"] and "109876543210987" not in ev["message"]
    assert ev["refs"] == ["109876543210987"]
    assert "webhook_like" in ev["trace"]


def test_no_recursion_when_capture_logs(monkeypatch):
    calls = []
    real = monitoring.capture_log_record

    def noisy(record):
        calls.append(record.getMessage())
        logging.getLogger("app.services.other").error("Échec imbriqué")
        return real(record)
    monkeypatch.setattr(monitoring, "capture_log_record", noisy)
    logging.getLogger("app.services.billing").error("Échec premier")
    assert calls == ["Échec premier"] and [e["title"] for e in _queued()] == ["Échec premier"]


def test_capture_never_raises(monkeypatch):
    monkeypatch.setattr(monitoring, "_push", lambda event: 1 / 0)
    assert monitoring.capture("API", title="x") is None
    logging.getLogger("app.x").error("Échec")  # le journal continue de fonctionner


def test_redis_down_falls_back_to_memory(monkeypatch):
    class Broken:
        def pipeline(self):
            raise ConnectionError("redis")
    monkeypatch.setattr(monitoring, "_queue_mode", lambda: "redis")
    monkeypatch.setattr(monitoring, "_client", lambda: Broken())
    monkeypatch.setattr(monitoring, "_redis_down_until", 0.0)
    monitoring.capture("APP", title="sans redis")
    assert [e["title"] for e in _queued()] == ["sans redis"]
    assert monitoring._redis_down_until > 0


# ---------------------------------------------------------------- API, tâches, navigateur

@pytest.mark.asyncio
async def test_api_crash_is_captured_with_route_and_shop(db_session):
    async def boom(item_id: int, user: CurrentUser = Depends(get_current_user)):
        raise ValueError("division impossible token=EAAGabcdef1234567890xyzXYZ")
    app.router.routes.insert(0, APIRoute("/api/v1/zz-test/{item_id}", boom, methods=["GET"], dependency_overrides_provider=app))
    tenant_id = uuid.uuid4()
    token = create_access_token(uuid.uuid4(), tenant_id, "ADMIN")
    try:
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as ac:
            r = await ac.get("/api/v1/zz-test/42", headers={"Authorization": f"Bearer {token}"})
    finally:
        app.router.routes.pop(0)
    assert r.status_code == 500 and r.json() == {"detail": "Erreur interne. L'équipe technique est prévenue."}
    [ev] = [e for e in _queued() if e["source"] == "API"]
    assert ev["title"] == "Erreur 500 sur GET /api/v1/zz-test/{item_id}" and ev["exc_type"] == "ValueError"
    assert ev["tenant_id"] == str(tenant_id) and "EAAGabcdef" not in ev["trace"]
    assert ev["location"].startswith("app/tests/test_lot67_monitoring.py:") and "(boom)" in ev["location"]


def test_task_failure_capture_and_signals():
    from celery.signals import task_failure, task_postrun

    import app.workers.celery_app  # noqa: F401 — le worker branche le monitoring à l'import
    assert task_failure.receivers and task_postrun.receivers
    try:
        raise KeyError("orders")
    except KeyError as exc:
        monitoring.capture_task_failure("app.workers.order_reminders.check_order_reminders_task", exc)
    [ev] = _queued()
    assert ev["title"] == "Tâche « Relances de commandes » en échec : KeyError"
    assert ev["context"] == "app.workers.order_reminders.check_order_reminders_task"


def test_every_scheduled_task_has_a_label_and_monitoring_runs_every_minute():
    from app.workers.celery_app import celery_app

    tasks = {e["task"]: e["schedule"] for e in celery_app.conf.beat_schedule.values()}
    assert set(tasks) <= set(monitoring.TASK_LABELS)
    assert monitoring_status.task_interval_minutes(tasks["app.workers.monitoring.drain_task"]) == 1
    assert monitoring_status.task_interval_minutes(tasks["app.workers.prospect_emails.send_prospect_email_task"]) == 3
    assert monitoring_status.task_interval_minutes(tasks["app.workers.order_reminders.check_order_reminders_task"]) == 60
    assert monitoring_status.task_interval_minutes(tasks["app.workers.monitoring.purge_task"]) == 1440


def test_tasks_status_late_failed_and_unseen():
    runs = {
        "app.workers.order_reminders.check_order_reminders_task": {"state": "SUCCESS", "ended": (NOW - timedelta(minutes=30)).isoformat()},
        "app.workers.billing.check_billing_task": {"state": "SUCCESS", "ended": (NOW - timedelta(hours=3)).isoformat()},
        "app.workers.followups.check_followups_task": {"state": "FAILURE", "ended": (NOW - timedelta(minutes=2)).isoformat()},
    }
    by = {t["task"]: t for t in monitoring_status.tasks_status(NOW, runs)}
    assert by["app.workers.order_reminders.check_order_reminders_task"]["status"] == "ok"
    assert by["app.workers.billing.check_billing_task"]["status"] == "warn"  # horaire, pas vue depuis 3 h
    assert by["app.workers.followups.check_followups_task"]["status"] == "ko"
    assert by["app.workers.reports.check_monthly_reports_task"]["status"] == "idle"


@pytest.mark.asyncio
async def test_browser_errors_endpoint(client, db_session):
    tenant_id = uuid.uuid4()
    token = create_access_token(uuid.uuid4(), tenant_id, "ADMIN")
    body = {"page": "dashboard", "message": "TypeError: Cannot read properties of undefined (reading 'items')",
            "source": "https://agenc-ai.com/bob/dashboard/", "line": 4242, "stack": "TypeError: x\n    at loadAppointments (https://agenc-ai.com/bob/dashboard/:4242:9)"}
    ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
    r = await client.post("/api/v1/monitoring/client-error", json=body, headers={"Authorization": f"Bearer {token}", "User-Agent": ua})
    assert r.status_code == 202 and r.json() == {"accepted": True}
    [ev] = _queued()
    assert ev["source"] == "BROWSER" and ev["browser"] == "Safari iPhone" and ev["tenant_id"] == str(tenant_id)
    assert ev["location"] == "dashboard/index.html · loadAppointments" and ev["context"] == "Tableau de bord"
    for noise in ({**body, "message": "TypeError: Failed to fetch"}, {**body, "message": "Script error."},
                  {**body, "source": "chrome-extension://abc/x.js"}, {**body, "page": "inconnue"}):
        assert (await client.post("/api/v1/monitoring/client-error", json=noise)).json() == {"accepted": False}
    assert len(_queued()) == 1
    for _ in range(25):
        r = await client.post("/api/v1/monitoring/client-error", json=body)
    assert r.json() == {"accepted": False}  # 20 au plus par adresse et par 10 minutes


# ---------------------------------------------------------------- regroupement, statuts, réouverture

@pytest.mark.asyncio
async def test_ingest_groups_counts_shops_and_reopens(db_session):
    awa = await _tenant(db_session, "Boutique Awa")
    auto = await _tenant(db_session, "Auto Plus")
    db_session.add(WhatsAppAccount(tenant_id=auto.id, waba_id="w", phone_number_id="109876543210987", system_user_token="t"))
    await db_session.commit()
    events = [_event(message=f"Relance impossible (boutique {awa.id})", refs=[str(awa.id)], at=NOW - timedelta(minutes=5)),
              _event(message="Relance impossible", tenant_id=awa.id, at=NOW - timedelta(minutes=3)),
              _event(title="Meta refuse", source="SEND_WHATSAPP", refs=["109876543210987"], meta_code="190", at=NOW - timedelta(minutes=2))]
    report = await monitoring.ingest(db_session, events, NOW)
    assert report == {"events": 3, "new": 2, "reopened": 0}
    issues = {i.source: i for i in (await db_session.execute(select(MonitorIssue))).scalars()}
    task = issues["TASK"]
    assert task.count == 2 and task.status == "NEW" and task.tenant_ids == [str(awa.id)]
    assert monitoring._utc(task.first_seen) == NOW - timedelta(minutes=5) and monitoring._utc(task.last_seen) == NOW - timedelta(minutes=3)
    assert issues["SEND_WHATSAPP"].tenant_ids == [str(auto.id)]  # retrouvée par le numéro WhatsApp Meta

    task.status, task.resolved_at, task.alerted_at = "RESOLVED", NOW, NOW
    await db_session.commit()
    await monitoring.ingest(db_session, [_event(at=NOW - timedelta(minutes=1))], NOW)  # arrivée avant la correction
    await db_session.refresh(task)
    assert task.status == "RESOLVED" and task.count == 3
    report = await monitoring.ingest(db_session, [_event(at=NOW + timedelta(minutes=10))], NOW + timedelta(minutes=10))
    await db_session.refresh(task)
    assert report["reopened"] == 1 and task.status == "REOPENED" and task.reopened_count == 1 and task.alerted_at is None
    assert (await db_session.execute(select(func.count(MonitorOccurrence.id)))).scalar_one() == 5


@pytest.mark.asyncio
async def test_drain_reads_the_queue(db_session):
    monitoring.capture("APP", title="Une erreur")
    report = await monitoring.drain(db_session)
    assert report["events"] == 1 and _queued() == []
    assert (await monitoring.drain(db_session))["events"] == 0


# ---------------------------------------------------------------- emails

async def _admins(db):
    db.add_all([SuperAdminUser(email="koffi@agencai.ci", hashed_password="x", full_name="Koffi"),
                SuperAdminUser(email="ancien@agencai.ci", hashed_password="x", full_name="Ancien", active=False)])
    await db.commit()


@pytest.mark.asyncio
async def test_new_error_email_once_with_cooldown(db_session):
    await _admins(db_session)
    sent = []
    send = lambda **kw: sent.append(kw)  # noqa: E731
    await monitoring.ingest(db_session, [_event(title="Meta refuse le message : jeton d'accès expiré ou invalide (code 190)",
                                                source="SEND_WHATSAPP", meta_code="190")] * 3, NOW)
    report = await monitoring.send_alerts(db_session, NOW, send)
    assert report == {"new": 1, "spikes": 0, "emails": 1}
    assert [m["to"] for m in sent] == ["koffi@agencai.ci"]
    assert sent[0]["subject"] == "Bob — nouvelle erreur technique : Meta refuse le message : jeton d'accès expiré ou invalide (code 190)"
    body = sent[0]["body"]
    assert "Reconnecter WhatsApp dans Intégrations" in body and "3 fois" in body and "/superadmin/#monitoring" in body
    # nouvelles occurrences de la même erreur : aucun nouvel email
    await monitoring.ingest(db_session, [_event(title="Meta refuse le message : jeton d'accès expiré ou invalide (code 190)",
                                                source="SEND_WHATSAPP", meta_code="190")], NOW + timedelta(minutes=1))
    # deux nouvelles erreurs 5 min plus tard : attendent la fin des 10 minutes, puis UN email pour les deux
    await monitoring.ingest(db_session, [_event(title="A"), _event(title="B", location="app/y.py:1 (f)")], NOW + timedelta(minutes=5))
    assert (await monitoring.send_alerts(db_session, NOW + timedelta(minutes=5), send))["emails"] == 0
    report = await monitoring.send_alerts(db_session, NOW + timedelta(minutes=11), send)
    assert report["new"] == 2 and len(sent) == 2 and sent[1]["subject"] == "Bob — 2 nouvelles erreurs techniques"
    assert (await monitoring.send_alerts(db_session, NOW + timedelta(minutes=30), send))["emails"] == 0


@pytest.mark.asyncio
async def test_spike_email_once_per_six_hours(db_session):
    await _admins(db_session)
    sent = []
    send = lambda **kw: sent.append(kw)  # noqa: E731
    burst = [_event(at=NOW - timedelta(minutes=i)) for i in range(25)]
    await monitoring.ingest(db_session, burst, NOW)
    await monitoring.send_alerts(db_session, NOW, send)
    subjects = [m["subject"] for m in sent]
    assert subjects == ["Bob — nouvelle erreur technique : Relance impossible",
                        "Bob — pic d'erreurs : Relance impossible (25 fois en 1 h)"]
    await monitoring.ingest(db_session, [_event(at=NOW + timedelta(minutes=30 + i)) for i in range(25)], NOW + timedelta(hours=1))
    await monitoring.send_alerts(db_session, NOW + timedelta(hours=1), send)
    assert len(sent) == 2  # moins de 6 h après le premier pic
    await monitoring.ingest(db_session, [_event(at=NOW + timedelta(hours=7, minutes=-i)) for i in range(21)], NOW + timedelta(hours=7))
    await monitoring.send_alerts(db_session, NOW + timedelta(hours=7), send)
    assert len(sent) == 3


@pytest.mark.asyncio
async def test_below_threshold_no_spike(db_session):
    await _admins(db_session)
    sent = []
    await monitoring.ingest(db_session, [_event(at=NOW - timedelta(minutes=i)) for i in range(19)], NOW)
    report = await monitoring.send_alerts(db_session, NOW, lambda **kw: sent.append(kw))
    assert report["spikes"] == 0 and len(sent) == 1


@pytest.mark.asyncio
async def test_purge_after_30_days(db_session):
    await monitoring.ingest(db_session, [_event(title="vieille", at=NOW - timedelta(days=40)),
                                         _event(title="récente", location="app/z.py:1 (f)", at=NOW - timedelta(days=40)),
                                         _event(title="récente", location="app/z.py:1 (f)", at=NOW - timedelta(days=2))], NOW)
    report = await monitoring.purge(db_session, NOW)
    assert report == {"occurrences": 2, "issues": 1}
    titles = [i.title for i in (await db_session.execute(select(MonitorIssue))).scalars()]
    assert titles == ["récente"]
    assert (await db_session.execute(select(func.count(MonitorOccurrence.id)))).scalar_one() == 1


# ---------------------------------------------------------------- explications

def test_explain_hints():
    issue = MonitorIssue(source="SEND_WHATSAPP", title="Meta refuse", meta_code="131047", location="app/integrations/whatsapp/client.py:150 (_post)")
    info = monitoring.explain(issue)
    assert "24 h" in info["consequence"] and info["where"] == "Envoi WhatsApp"
    smtp = MonitorIssue(source="SEND_EMAIL", title="Échec de l'envoi d'email", exc_type="SMTPAuthenticationError",
                        last_message="(535, b'5.7.8 Username and Password not accepted')", location="app/services/email_service.py:126 (send_email)")
    assert "SMTP_PASSWORD" in monitoring.explain(smtp)["hint"] and "identifiants refusés" in monitoring.explain(smtp)["consequence"]
    task = MonitorIssue(source="TASK", title="x", context="app.workers.billing.check_billing_task")
    assert monitoring.explain(task)["where"] == "Tâche automatique · Échéances d'abonnement"
    api = MonitorIssue(source="API", title="x", context="GET /api/v1/contracts/export.xlsx")
    assert monitoring.explain(api)["where"] == "API · GET /api/v1/contracts/export.xlsx"
    webhook = MonitorIssue(source="APP", title="x", location="app/api/webhooks/whatsapp.py:10 (receive_webhook)")
    assert monitoring.explain(webhook)["where"] == "Webhook WhatsApp → réponse de Bob"
    crypto = MonitorIssue(source="APP", title="Jeton chiffré illisible : la clé de chiffrement a-t-elle changé ?")
    assert "SECRET_KEY" in monitoring.explain(crypto)["hint"]


def test_run_test_masks_errors():
    def refuse(*args):
        raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted password=abcd efgh")
    from app.core.config import get_settings

    s = get_settings()
    old = (s.smtp_host, s.smtp_password)
    s.smtp_host, s.smtp_password = "smtp.test", "abcd efgh"
    try:
        result = monitoring_status.run_test("smtp", smtp=refuse)
        assert result["ok"] is False and "abcd" not in result["detail"] and "SMTPAuthenticationError" in result["detail"]
        assert monitoring_status.run_test("smtp", smtp=lambda *a: None) == {"ok": True, "detail": "Connexion et identifiants acceptés"}
    finally:
        s.smtp_host, s.smtp_password = old


# ---------------------------------------------------------------- Super Admin

@pytest.mark.asyncio
async def test_superadmin_monitoring_requires_login(client, db_session):
    for path in ("/api/v1/superadmin/monitoring", "/api/v1/superadmin/monitoring/status"):
        assert (await client.get(path)).status_code == 401
    tenant_token = create_access_token(uuid.uuid4(), uuid.uuid4(), "ADMIN")
    assert (await client.get("/api/v1/superadmin/monitoring", headers={"Authorization": f"Bearer {tenant_token}"})).status_code == 401


@pytest.mark.asyncio
async def test_superadmin_list_detail_and_status(client, db_session):
    headers = await _admin(client, db_session, name="Koffi Yao")
    awa = await _tenant(db_session, "Boutique Awa")
    monitoring.capture("SEND_WHATSAPP", title="Meta refuse le message : jeton d'accès expiré ou invalide (code 190)", meta_code="190",
                       message="Error validating access token=EAAGabcdef1234567890xyzXYZ", tenant_id=awa.id,
                       location="app/integrations/whatsapp/client.py:150 (_post)", origin="app/api/webhooks/whatsapp.py:575 (receive_webhook)",
                       trace="Traceback\n  token=EAAGabcdef1234567890xyzXYZ")
    monitoring.capture("API", title="Erreur 500 sur GET /api/v1/orders", context="GET /api/v1/orders", exc_type="KeyError")
    d = (await client.get("/api/v1/superadmin/monitoring", headers=headers)).json()  # vide la file d'abord
    assert d["counts"] == {"todo": 2, "new": 2, "progress": 0, "resolved": 0}
    wa = next(i for i in d["issues"] if i["source"] == "SEND_WHATSAPP")
    assert wa["shops"] == ["Boutique Awa"] and wa["status_label"] == "Nouvelle" and wa["source_label"] == "Envoi WhatsApp"
    only_api = (await client.get("/api/v1/superadmin/monitoring?source=API", headers=headers)).json()["issues"]
    assert [i["source"] for i in only_api] == ["API"]
    assert (await client.get("/api/v1/superadmin/monitoring?filter=nope", headers=headers)).status_code == 400

    detail = (await client.get(f"/api/v1/superadmin/monitoring/issues/{wa['id']}", headers=headers)).json()
    assert detail["consequence"] == "Les clients de cette boutique ne reçoivent plus les réponses de Bob."
    assert detail["hint"].startswith("Reconnecter WhatsApp") and "EAAGabcdef" not in detail["trace"]
    assert detail["occurrences"][0]["shop"] == "Boutique Awa" and "EAAGabcdef" not in detail["occurrences"][0]["message"]

    r = await client.post(f"/api/v1/superadmin/monitoring/issues/{wa['id']}/status", headers=headers, json={"status": "IN_PROGRESS"})
    assert r.json()["status_label"] == "En cours" and r.json()["assignee"] == "Koffi Yao"
    r = await client.post(f"/api/v1/superadmin/monitoring/issues/{wa['id']}/status", headers=headers, json={"status": "RESOLVED"})
    assert r.json()["status"] == "RESOLVED" and r.json()["resolved_by"] == "Koffi Yao"
    assert (await client.post(f"/api/v1/superadmin/monitoring/issues/{wa['id']}/status", headers=headers, json={"status": "X"})).status_code == 400
    assert (await client.get(f"/api/v1/superadmin/monitoring/issues/{uuid.uuid4()}", headers=headers)).status_code == 404
    d = (await client.get("/api/v1/superadmin/monitoring?filter=resolved", headers=headers)).json()
    assert d["counts"] == {"todo": 1, "new": 1, "progress": 0, "resolved": 1} and [i["id"] for i in d["issues"]] == [wa["id"]]

    overview = (await client.get("/api/v1/superadmin/overview", headers=headers)).json()
    assert overview["todo"][0]["kind"] == "MONITORING" and overview["todo"][0]["section"] == "monitoring"
    assert overview["todo"][0]["title"] == "1 erreur technique à traiter" and overview["kpis"]["monitoring_new"] == 1


@pytest.mark.asyncio
async def test_service_status(client, db_session):
    headers = await _admin(client, db_session)
    from app.core.config import get_settings

    s = get_settings()
    old = s.smtp_host
    s.smtp_host = ""
    try:
        d = (await client.get("/api/v1/superadmin/monitoring/status", headers=headers)).json()
        checks = {c["key"]: c for c in d["checks"]}
        assert list(checks) == ["api", "db", "redis", "tasks", "smtp", "gmail", "ai", "whatsapp"]
        assert checks["db"]["status"] == "ok" and checks["smtp"]["status"] == "ko" and checks["redis"]["status"] == "idle"
        assert checks["tasks"]["status"] == "idle" and len(d["tasks"]) >= 15
        s.smtp_host = "smtp.test"
        monitoring.mark("smtp_ok", datetime.now(timezone.utc) - timedelta(minutes=4))
        checks = {c["key"]: c for c in (await client.get("/api/v1/superadmin/monitoring/status", headers=headers)).json()["checks"]}
        assert checks["smtp"] == {"key": "smtp", "name": "Envoi d'emails (SMTP)", "status": "ok", "detail": "Dernier envoi réussi il y a 4 min", "test": True}
        monitoring.mark("smtp_fail")
        checks = {c["key"]: c for c in (await client.get("/api/v1/superadmin/monitoring/status", headers=headers)).json()["checks"]}
        assert checks["smtp"]["status"] == "ko"
    finally:
        s.smtp_host = old
    assert (await client.post("/api/v1/superadmin/monitoring/checks/rien/test", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_status_flags_expired_whatsapp_token(db_session):
    awa = await _tenant(db_session, "Boutique Awa")
    await monitoring.ingest(db_session, [_event(title="Meta refuse", source="SEND_WHATSAPP", meta_code="190", tenant_id=awa.id)],
                            datetime.now(timezone.utc))
    checks = {c["key"]: c for c in (await monitoring_status.checks(db_session))["checks"]}
    assert checks["whatsapp"]["status"] == "ko" and checks["whatsapp"]["detail"] == "1 boutique : jeton expiré (Boutique Awa)"


def test_channel_marks_on_success():
    from app.core.config import get_settings

    s = get_settings()
    old = s.smtp_host
    s.smtp_host = "smtp.test"

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, *a): pass
    import app.services.email_service as es
    real = es.smtplib.SMTP
    es.smtplib.SMTP = FakeSMTP
    try:
        assert es.send_email(to="a@b.ci", subject="s", body="b") is True
    finally:
        es.smtplib.SMTP, s.smtp_host = real, old
    assert "smtp_ok" in monitoring.read_marks()


# ---------------------------------------------------------------- pages

def test_superadmin_page_has_monitoring_section():
    assert 'data-section="monitoring"' in SA_HTML and 'id="monitoring-section"' in SA_HTML
    assert 'id="nav-count-monitoring"' in SA_HTML and "Monitoring technique" in SA_HTML
    assert "Les secrets sont toujours masqués" in SA_HTML
    for label in ("Je m'en occupe", "Marquer corrigée", "Où", "Conséquence", "Piste", "État du service"):
        assert label in SA_HTML
    assert 'location.hash === "#monitoring"' in SA_HTML
    for forbidden in ("alert(", "confirm(", "prompt("):
        assert not re.search(r"(?<![\w.])" + re.escape(forbidden), SA_HTML.replace("saConfirm(", ""))


@pytest.mark.parametrize("path, page", [("app/static/dashboard/index.html", "dashboard"), ("app/static/instant-demo/index.html", "demo"),
                                        ("app/static/superadmin/index.html", "superadmin")])
def test_pages_report_js_errors(path, page):
    html = Path(path).read_text(encoding="utf-8")
    block = html[html.index("Lot 67 — erreurs du navigateur"):]
    block = block[:block.index("</script>")]
    assert f'page: "{page}"' in block and "/api/v1/monitoring/client-error" in block
    assert "unhandledrejection" in block and "keepalive: true" in block
    assert not re.search(r"\b(let|const)\b|=>", block)  # règle du lot 56 : rien qui casse le démarrage
    head = html[:html.index("</head>")]
    assert "Lot 67 — erreurs du navigateur" in head  # posé avant tout le reste
