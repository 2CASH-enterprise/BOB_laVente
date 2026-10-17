"""
Lot 68 — modèles WhatsApp approuvés par Meta : catalogue (textes validés le 11/10), création chez Meta, statut
(webhook et vérification), envoi hors fenêtre de 20 h avec l'email en secours, page « Payer ma commande »,
livraison (en route, livrée), interrupteur de la plateforme et exclusion d'une boutique.
"""
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.integrations.whatsapp.client import MetaGraphError, WhatsAppClient
from app.models.appointment_request import AppointmentRequest
from app.models.audit_log import AuditLog
from app.models.conversation import Message
from app.models.delivery import Delivery
from app.models.insurance_contract import InsuranceContract
from app.models.order import OrderStatus
from app.models.preregistration import PlatformSettings
from app.models.tenant import Tenant
from app.models.whatsapp_account import WhatsAppAccount
from app.models.whatsapp_template import WhatsAppTemplate
from app.services import delivery_notice, wa_templates as wt
from app.services.business_type import CAR_DEALERSHIP, INSURANCE_BROKER, ONLINE_STORE
from app.tests.test_lot53_courtier import _cabinet, _headers
from app.tests.test_lot61_prospection import _admin
from app.tests.test_lot63_relances_commandes import NOW, _conversation, _customer, _order, _recorder, _shop

DASH = Path("app/static/dashboard/index.html").read_text(encoding="utf-8")
SA = Path("app/static/superadmin/index.html").read_text(encoding="utf-8")


async def _on(db, enabled=True):
    row = await db.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1, prereg_mode=False)
        db.add(row)
    row.wa_templates_enabled = enabled
    await db.commit()


async def _approve(db, tenant, key, form="VOUS", status="APPROVED", category="UTILITY"):
    db.add(WhatsAppTemplate(tenant_id=tenant.id, key=key, form=form, name=wt.template_name(key, form), status=status, category=category))
    await db.commit()


@pytest.fixture
def meta_sends(monkeypatch):
    sent = []

    async def fake(self, to, template_name, language="fr", components=None):
        sent.append({"to": to, "name": template_name, "language": language, "components": components})
        return {"messages": [{"id": "wamid.x"}]}
    monkeypatch.setattr(WhatsAppClient, "send_template_message", fake)
    return sent


class FakeMeta:
    def __init__(self, existing=(), fail=()):
        self.existing, self.fail, self.created = list(existing), set(fail), []

    async def list_templates(self):
        return self.existing

    async def create_template(self, payload):
        if payload["name"] in self.fail:
            raise MetaGraphError("Meta a répondu 400 (code 100, Invalid parameter token=EAAGabcdef1234567890xyzXYZ)")
        self.created.append(payload)
        return {"id": f"id-{payload['name']}", "status": "PENDING", "category": "UTILITY"}


# ---------------------------------------------------------------- catalogue

VALIDATED = {
    ("rdv_rappel", "VOUS"): "Bonjour {{1}}, petit rappel de votre rendez-vous chez {{2}} demain, {{3}}, à {{4}}. Pour le déplacer ou l'annuler, répondez simplement à ce message.",
    ("rdv_absence", "TU"): "Bonjour {{1}}, on ne t'a pas vu à ton rendez-vous chez {{2}} le {{3}}. Tu veux en fixer un autre ? Réponds simplement à ce message avec le jour qui t'arrange.",
    ("commande_paiement", "VOUS"): "Bonjour {{1}}, votre commande {{2}} chez {{3}} ({{4}}) est bien enregistrée et attend votre paiement. Une question ? Répondez simplement à ce message.",
    ("contrat_echeance", "VOUS"): "Bonjour {{1}}, votre contrat {{2}} souscrit auprès de {{3}} arrive à échéance le {{4}}. Pour le renouveler ou poser une question, répondez simplement à ce message.",
    ("livraison_expediee", "TU"): "Bonjour {{1}}, ta commande {{2}} chez {{3}} est en route. Livraison prévue : {{4}}. Un souci ? Réponds simplement à ce message.",
    ("livraison_livree", "VOUS"): "Bonjour {{1}}, votre commande {{2}} chez {{3}} a bien été livrée. Merci pour votre confiance ! Un souci avec la commande ? Répondez simplement à ce message.",
}


def test_texts_are_the_validated_ones():
    for (key, form), text in VALIDATED.items():
        assert wt.CATALOG[key][form] == text


@pytest.mark.parametrize("key", list(wt.CATALOG))
def test_catalog_respects_meta_rules(key):
    spec = wt.CATALOG[key]
    for form in (f for f in (wt.VOUS, wt.TU) if f in spec):  # lot 80 : « nouveau bien » n'existe qu'au vouvoiement
        text = spec[form]
        numbers = [int(n) for n in re.findall(r"\{\{(\d+)\}\}", text)]
        assert numbers == list(range(1, len(numbers) + 1)) == list(range(1, len(spec["example"]) + 1))
        assert not text.startswith("{{") and not text.rstrip(".!?").endswith("}}")
        assert "}}{{" not in text.replace(" ", "") and "prix" not in text.lower() and "promo" not in text.lower()
        payload = wt.create_payload(key, form)
        assert payload["name"] == f"bob_{key}_{form.lower()}" and re.fullmatch(r"[a-z0-9_]+", payload["name"])
        assert payload["category"] == ("MARKETING" if key == "nouveau_bien" else "UTILITY") and payload["language"] == "fr"
        assert payload["components"][0]["example"]["body_text"] == [spec["example"]]
    if spec.get("button"):
        button = wt.create_payload(key, wt.VOUS)["components"][1]["buttons"][0]
        assert button == {"type": "URL", "text": "Payer ma commande", "url": "https://agenc-ai.com/bob/c/{{1}}",
                          "example": ["https://agenc-ai.com/bob/c/K7P2QX9M3D"]}


def test_templates_per_sector():
    shop, dealer, broker = Tenant(business_type=ONLINE_STORE), Tenant(business_type=CAR_DEALERSHIP), Tenant(business_type=INSURANCE_BROKER)
    assert wt.wanted(shop) == [("commande_paiement", "VOUS"), ("commande_paiement", "TU"), ("livraison_expediee", "VOUS"),
                               ("livraison_expediee", "TU"), ("livraison_livree", "VOUS"), ("livraison_livree", "TU")]
    assert wt.wanted(dealer) == [("rdv_rappel", "VOUS"), ("rdv_absence", "VOUS")]  # le tutoiement n'existe qu'en Commerce
    assert wt.wanted(broker) == [("rdv_rappel", "VOUS"), ("contrat_echeance", "VOUS")]


def test_render_and_clean_values():
    assert wt.clean_value("  Awa\n  Koné\t ") == "Awa Koné" and wt.clean_value("") == "—" and wt.clean_value(None) == "—"
    assert wt.render("livraison_livree", "TU", ["Awa", "AB12", "Boutique"]).startswith("Bonjour Awa, ta commande AB12 chez Boutique")
    assert wt.first_name(type("C", (), {"first_name": "  "})()) == "cher client"
    code = wt.new_pay_code()
    assert len(code) == 10 and set(code) <= set(wt.PAY_CODE_ALPHABET) and code != wt.new_pay_code()


# ---------------------------------------------------------------- création chez Meta

@pytest.mark.asyncio
async def test_sync_does_nothing_while_switched_off_or_blocked(db_session):
    tenant = await _shop(db_session)
    meta = FakeMeta()
    report = await wt.sync(db_session, tenant, client=meta)
    assert report["error"] == "Les modèles WhatsApp sont éteints pour toute la plateforme." and meta.created == []
    await _on(db_session)
    tenant.wa_templates_allowed = False
    await db_session.commit()
    assert (await wt.sync(db_session, tenant, client=meta))["error"] == "Les modèles WhatsApp sont désactivés pour cette boutique."
    tenant.wa_templates_allowed = True
    await db_session.commit()
    no_wa = await _cabinet(db_session, business_type=ONLINE_STORE, with_account=False)
    assert (await wt.sync(db_session, no_wa, client=meta))["error"] == "Aucun numéro WhatsApp n'est connecté à cette boutique."
    assert meta.created == []


@pytest.mark.asyncio
async def test_sync_creates_missing_and_reads_existing(db_session):
    await _on(db_session)
    tenant = await _shop(db_session)
    meta = FakeMeta(existing=[{"id": "111", "name": "bob_commande_paiement_vous", "language": "fr", "status": "APPROVED", "category": "UTILITY",
                               "rejected_reason": "NONE"},
                              {"id": "222", "name": "bob_livraison_livree_tu", "language": "fr", "status": "REJECTED", "category": "UTILITY",
                               "rejected_reason": "INVALID_FORMAT"},
                              {"id": "333", "name": "bob_commande_paiement_tu", "language": "en", "status": "APPROVED"}],
                    fail={"bob_livraison_expediee_tu"})
    report = await wt.sync(db_session, tenant, client=meta)
    assert report == {"ok": False, "created": 3, "updated": 2, "errors": 1, "error": None}
    assert sorted(p["name"] for p in meta.created) == ["bob_commande_paiement_tu", "bob_livraison_expediee_vous", "bob_livraison_livree_vous"]
    rows = await wt.rows_of(db_session, tenant.id)
    assert rows["bob_commande_paiement_vous"].status == "APPROVED" and rows["bob_commande_paiement_vous"].meta_id == "111"
    assert rows["bob_commande_paiement_vous"].reason is None
    assert rows["bob_livraison_livree_tu"].status == "REJECTED" and rows["bob_livraison_livree_tu"].reason == "INVALID_FORMAT"
    assert rows["bob_commande_paiement_tu"].status == "PENDING"  # la version anglaise ne compte pas
    failed = rows["bob_livraison_expediee_tu"]
    assert failed.status == "ERROR" and "EAAGabcdef" not in failed.reason and "code 100" in failed.reason
    # Deuxième passage : rien n'est recréé
    meta2 = FakeMeta(existing=[{"id": r.meta_id, "name": n, "language": "fr", "status": "PENDING"} for n, r in rows.items() if r.meta_id])
    assert (await wt.sync(db_session, tenant, create=False, client=meta2))["created"] == 0 and meta2.created == []


@pytest.mark.asyncio
async def test_sync_reports_meta_unreachable(db_session):
    await _on(db_session)
    tenant = await _shop(db_session)

    class Down(FakeMeta):
        async def list_templates(self):
            raise ConnectionError("https://graph.facebook.com/?access_token=EAAGabcdef1234567890xyzXYZ")
    report = await wt.sync(db_session, tenant, client=Down())
    assert report["error"] == "Meta est injoignable pour le moment" and report["ok"] is False


@pytest.mark.asyncio
async def test_webhook_updates_status_and_category(client, db_session):
    await _on(db_session)
    tenant = await _shop(db_session)
    account = (await db_session.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalar_one()
    account.waba_id = f"waba-{uuid.uuid4().hex[:6]}"
    await db_session.commit()
    await _approve(db_session, tenant, "commande_paiement", status="PENDING")
    row = (await db_session.execute(select(WhatsAppTemplate).where(WhatsAppTemplate.tenant_id == tenant.id))).scalar_one()
    row.meta_id = "987"
    await db_session.commit()
    payload = {"object": "whatsapp_business_account", "entry": [{"id": account.waba_id, "changes": [
        {"field": "message_template_status_update", "value": {"event": "APPROVED", "message_template_id": 987,
                                                               "message_template_name": "bob_commande_paiement_vous", "reason": "NONE"}}]}]}
    r = await client.post("/webhooks/whatsapp", json=payload)
    assert r.json() == {"status": "template_status_recorded", "recorded": 1}
    await db_session.refresh(row)
    assert row.status == "APPROVED" and row.reason is None
    payload["entry"][0]["changes"] = [{"field": "template_category_update", "value": {
        "message_template_id": 987, "message_template_name": "autre_nom", "new_category": "MARKETING"}}]
    assert (await client.post("/webhooks/whatsapp", json=payload)).json()["recorded"] == 1  # retrouvé par son id Meta
    await db_session.refresh(row)
    assert row.category == "MARKETING"
    payload["entry"][0]["id"] = "inconnu"
    assert (await client.post("/webhooks/whatsapp", json=payload)).json()["recorded"] == 0


@pytest.mark.asyncio
async def test_refresh_pending_only_when_on(db_session):
    tenant = await _shop(db_session)
    await _approve(db_session, tenant, "commande_paiement", status="PENDING")
    seen = []

    def factory(account):
        seen.append(account.tenant_id)
        return FakeMeta(existing=[{"id": "1", "name": "bob_commande_paiement_vous", "language": "fr", "status": "APPROVED"}])
    assert await wt.refresh_pending(db_session, factory) == {"tenants": 0, "failed": 0}
    await _on(db_session)
    assert await wt.refresh_pending(db_session, factory) == {"tenants": 1, "failed": 0}
    rows = await wt.rows_of(db_session, tenant.id)
    assert rows["bob_commande_paiement_vous"].status == "APPROVED" and seen == [tenant.id]
    assert len(rows) == 1  # vérification seulement : aucun modèle créé


# ---------------------------------------------------------------- envoi

@pytest.mark.asyncio
async def test_send_only_approved_utility_and_logs_the_message(db_session, meta_sends):
    tenant = await _shop(db_session, address_form="TU")
    customer = await _customer(db_session, tenant, first="Awa\nKoné")
    conversation = await _conversation(db_session, tenant, customer)
    values = ["Awa", "AB12", "Boutique Awa", "15 000 XOF"]
    assert await wt.send(db_session, tenant, customer, conversation, "commande_paiement", values, button="K7P2QX9M3D") is False  # éteint
    await _on(db_session)
    assert await wt.send(db_session, tenant, customer, conversation, "commande_paiement", values, button="K7P2QX9M3D") is False  # pas de modèle
    await _approve(db_session, tenant, "commande_paiement", form="VOUS")
    assert await wt.send(db_session, tenant, customer, conversation, "commande_paiement", values) is False  # la boutique tutoie
    await _approve(db_session, tenant, "commande_paiement", form="TU", category="MARKETING")
    assert await wt.send(db_session, tenant, customer, conversation, "commande_paiement", values) is False  # reclassé marketing
    row = (await db_session.execute(select(WhatsAppTemplate).where(WhatsAppTemplate.name == "bob_commande_paiement_tu",
                                                                   WhatsAppTemplate.tenant_id == tenant.id))).scalar_one()
    row.category = "UTILITY"
    await db_session.commit()
    assert meta_sends == []
    assert await wt.send(db_session, tenant, customer, conversation, "commande_paiement", [wt.first_name(customer), "AB12", "Boutique Awa", ""],
                         button="K7P2QX9M3D", message_type="order_reminder") is True
    await db_session.commit()
    [sent] = meta_sends
    assert sent["name"] == "bob_commande_paiement_tu" and sent["language"] == "fr" and sent["to"] == customer.whatsapp_number
    assert sent["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": t} for t in ("Awa Koné", "AB12", "Boutique Awa", "—")]},
        {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "K7P2QX9M3D"}]}]
    msg = (await db_session.execute(select(Message).where(Message.conversation_id == conversation.id))).scalars().all()[-1]
    assert msg.message_type == "order_reminder" and msg.content.startswith("Bonjour Awa Koné, ta commande AB12 chez Boutique Awa (—)")
    assert msg.content.endswith("[Payer ma commande] https://agenc-ai.com/bob/c/K7P2QX9M3D")
    from app.models.messaging_settings import KillSwitch, TenantMessagingSettings

    guard = (await db_session.execute(select(TenantMessagingSettings).where(TenantMessagingSettings.tenant_id == tenant.id))).scalar_one()
    guard.kill_switch = KillSwitch.BLOCKED
    await db_session.commit()
    assert await wt.send(db_session, tenant, customer, conversation, "commande_paiement", values) is False  # arrêt d'urgence
    guard.kill_switch = KillSwitch.ALLOWED
    tenant.wa_templates_allowed = False
    await db_session.commit()
    assert await wt.send(db_session, tenant, customer, conversation, "commande_paiement", values) is False and len(meta_sends) == 1


@pytest.mark.asyncio
async def test_send_refused_by_meta_returns_false(db_session, monkeypatch):
    from app.integrations.whatsapp.client import WhatsAppSendError

    async def refuse(self, *a, **k):
        raise WhatsAppSendError(400, "code 132001")
    monkeypatch.setattr(WhatsAppClient, "send_template_message", refuse)
    await _on(db_session)
    tenant = await _shop(db_session)
    customer = await _customer(db_session, tenant)
    await _approve(db_session, tenant, "livraison_livree")
    assert await wt.send(db_session, tenant, customer, None, "livraison_livree", ["A", "B", "C"]) is False


# ---------------------------------------------------------------- relances branchées

@pytest.mark.asyncio
async def test_order_reminder_uses_the_template_when_the_window_is_closed(db_session, meta_sends):
    from app.services import order_reminders as rem

    await _on(db_session)
    tenant = await _shop(db_session)
    customer = await _customer(db_session, tenant, email="awa@example.com")
    conversation = await _conversation(db_session, tenant, customer, wrote_hours_ago=30)
    order = await _order(db_session, tenant, customer, hours_ago=25, conversation=conversation)
    order.reminders_sent, order.last_reminder_at = 1, NOW - timedelta(hours=13)
    await db_session.commit()
    sent, send_email, send_whatsapp = _recorder()
    report = await rem.run_for_tenant(db_session, tenant, NOW, send_email=send_email, send_whatsapp=send_whatsapp)
    assert report["email"] == 1 and meta_sends == []  # pas de modèle approuvé : l'email, comme avant
    order.reminders_sent = 0
    order.created_at = NOW - timedelta(hours=4)
    await db_session.commit()
    await _approve(db_session, tenant, "commande_paiement")
    report = await rem.run_for_tenant(db_session, tenant, NOW, send_email=send_email, send_whatsapp=send_whatsapp)
    assert report["whatsapp"] == 1 and len(sent["email"]) == 1 and sent["whatsapp"] == []
    await db_session.refresh(order)
    assert order.last_reminder_channel == "WHATSAPP" and order.pay_code and len(order.pay_code) == 10
    body, button = meta_sends[0]["components"]
    assert [p["text"] for p in body["parameters"]] == ["Fatou", str(order.id)[:8].upper(), "Boutique Awa", "15 000 XOF"]
    assert button["parameters"][0]["text"] == order.pay_code


@pytest.mark.asyncio
async def test_order_reminder_keeps_free_text_inside_the_window(db_session, meta_sends):
    from app.services import order_reminders as rem

    await _on(db_session)
    tenant = await _shop(db_session)
    await _approve(db_session, tenant, "commande_paiement")
    customer = await _customer(db_session, tenant)
    conversation = await _conversation(db_session, tenant, customer, wrote_hours_ago=5)
    await _order(db_session, tenant, customer, hours_ago=4, conversation=conversation)
    sent, send_email, send_whatsapp = _recorder()
    report = await rem.run_for_tenant(db_session, tenant, NOW, send_email=send_email, send_whatsapp=send_whatsapp)
    assert report["whatsapp"] == 1 and len(sent["whatsapp"]) == 1 and meta_sends == []


@pytest.mark.asyncio
async def test_appointment_eve_reminder_template(meta_sends):
    from app.tests.test_prospect_appointments import _eve_setup
    from app.workers import appointment_reminders

    engine, factory, evening = await _eve_setup(30)  # fenêtre fermée
    calls = []

    async def fake_template(db, tenant, customer, conversation, key, values, button=None, message_type=None):
        calls.append((key, values, message_type))
        return True
    first = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening, send=lambda **kw: True,
                                                           send_whatsapp=None, send_template=fake_template)
    second = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening + timedelta(minutes=15),
                                                            send=lambda **kw: True, send_template=fake_template)
    assert first["whatsapp"] == 1 and second["whatsapp"] == 0
    assert calls == [("rdv_rappel", ["cher client", "Auto Plus", "samedi 3 octobre", "10 h"], "appointment_reminder")]
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_show_followup_template(db_session, meta_sends):
    from app.models.appointment_request import OUTCOME_NO_SHOW
    from app.services import appointment_outcome
    from app.tests.test_appointment_outcome import _setup

    await _on(db_session)
    tenant, customer, car, appointment = await _setup(db_session, wrote_hours_ago=100, scheduled=datetime(2026, 10, 9, 15, 30, tzinfo=timezone.utc))
    customer.first_name = "Moussa"
    appointment.outcome = OUTCOME_NO_SHOW
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=f"pn-{uuid.uuid4().hex[:6]}", system_user_token="t"))
    await db_session.commit()
    await _approve(db_session, tenant, "rdv_absence")
    channel = await appointment_outcome.send_followup(db_session, tenant, appointment, datetime.now(timezone.utc), send_email=lambda **kw: True)
    assert channel == "WHATSAPP"
    assert [p["text"] for p in meta_sends[0]["components"][0]["parameters"]] == ["Moussa", "Auto Plus", "vendredi 9 octobre"]
    appointment.kind, appointment.followup_sent_at = "APPEL", None
    await db_session.commit()
    assert await appointment_outcome.send_followup(db_session, tenant, appointment, datetime.now(timezone.utc),
                                                   send_email=lambda **kw: True) != "WHATSAPP"  # un appel manqué : pas ce modèle
    assert len(meta_sends) == 1


@pytest.mark.asyncio
async def test_renewal_template_values(db_session):
    tenant = await _cabinet(db_session)
    contract = InsuranceContract(tenant_id=tenant.id, customer_id=uuid.uuid4(), branch="AUTO", expires_on=date(2026, 10, 31))
    customer = type("C", (), {"first_name": "Awa"})()
    from app.services.insurance_contracts import renewal_template_values

    from app.services.insurance import branch_label

    assert renewal_template_values(tenant, customer, contract) == ["Awa", branch_label("AUTO"), "Cabinet Kouassi", "31/10/2026"]
    source = Path("app/services/insurance_contracts.py").read_text(encoding="utf-8")
    block = source[source.index("customer_window_open(db, tenant.id, customer.id, now):\n                if await send_whatsapp"):]
    assert block.index('"contrat_echeance"') < block.index("reminder_email(db, tenant, customer, contract)")


# ---------------------------------------------------------------- livraison

@pytest.mark.asyncio
async def test_delivery_notice_once_per_step(client, db_session):
    tenant = await _shop(db_session)
    customer = await _customer(db_session, tenant, email="awa@example.com")
    conversation = await _conversation(db_session, tenant, customer, wrote_hours_ago=40)
    order = await _order(db_session, tenant, customer, hours_ago=2, conversation=conversation)
    headers = await _headers(client, tenant)
    mails = []
    import app.services.email_service as es
    real = es.send_email
    es.send_email = lambda **kw: mails.append(kw) or True
    try:
        r = await client.put(f"/api/v1/orders/{order.id}/delivery", headers=headers,
                             json={"status": "IN_TRANSIT", "estimated_delivery": "2026-10-15"})
        assert r.status_code == 200 and r.json()["notified_step"] == "SHIPPED" and r.json()["notified_by"] == "EMAIL"
        assert mails[0]["subject"] == f"Votre commande {str(order.id)[:8].upper()} est en route — Boutique Awa"
        assert "Livraison prévue : jeudi 15 octobre." in mails[0]["body"] and "Répondez simplement à cet email." in mails[0]["body"]
        r = await client.put(f"/api/v1/orders/{order.id}/delivery", headers=headers, json={"status": "PICKED_UP"})
        assert r.json()["notified_step"] is None and len(mails) == 1  # déjà prévenu pour cette étape
        r = await client.put(f"/api/v1/orders/{order.id}/delivery", headers=headers, json={"status": "DELIVERED", "notify_customer": False})
        assert r.json()["notified_by"] is None and len(mails) == 1
        r = await client.put(f"/api/v1/orders/{order.id}/delivery", headers=headers, json={"status": "DELIVERED"})
        assert r.json()["notified_step"] == "DELIVERED" and "a bien été livrée" in mails[1]["body"]
        r = await client.put(f"/api/v1/orders/{order.id}/delivery", headers=headers, json={"status": "DELIVERED"})
        assert r.json()["notified_step"] is None and len(mails) == 2
        assert (await client.put(f"/api/v1/orders/{order.id}/delivery", headers=headers, json={"status": "PERDU"})).status_code == 400
    finally:
        es.send_email = real


@pytest.mark.asyncio
async def test_delivery_notice_template_and_window(db_session, meta_sends):
    await _on(db_session)
    tenant = await _shop(db_session)
    customer = await _customer(db_session, tenant)
    conversation = await _conversation(db_session, tenant, customer, wrote_hours_ago=40)
    order = await _order(db_session, tenant, customer, hours_ago=2, conversation=conversation)
    await _approve(db_session, tenant, "livraison_expediee")
    delivery = Delivery(tenant_id=tenant.id, order_id=order.id, status="IN_TRANSIT")
    db_session.add(delivery)
    await db_session.flush()
    result = await delivery_notice.notify(db_session, tenant, order, delivery, "u1")
    assert result == {"step": "SHIPPED", "channel": "WHATSAPP"} and delivery.shipped_notified_at is not None
    assert [p["text"] for p in meta_sends[0]["components"][0]["parameters"]][3] == "dans les prochains jours"
    delivery.status = "DELIVERED"
    assert (await delivery_notice.notify(db_session, tenant, order, delivery, "u1")) == {"step": "DELIVERED", "channel": None}
    assert delivery.delivered_notified_at is None  # personne prévenu : on pourra réessayer


# ---------------------------------------------------------------- page « Payer ma commande »

@pytest.mark.asyncio
async def test_pay_page(client, db_session):
    tenant = await _shop(db_session, name="Boutique <Awa>", payment_link='https://pay.wave.com/m/awa?r="x"')
    account = (await db_session.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalar_one()
    account.display_phone_number = "+225 07 07 00 00 01"
    customer = await _customer(db_session, tenant)
    order = await _order(db_session, tenant, customer, hours_ago=4)
    code = await wt.ensure_pay_code(db_session, order)
    await db_session.commit()
    r = await client.get(f"/c/{code}")
    assert r.status_code == 200 and r.headers["x-robots-tag"] == "noindex, nofollow"
    html = r.text
    assert "Boutique &lt;Awa&gt;" in html and "<Awa>" not in html
    assert f"Votre commande {str(order.id)[:8].upper()}" in html and "Robe wax" in html and "15 000 XOF" in html
    assert 'href="https://pay.wave.com/m/awa?r=&quot;x&quot;"' in html and "https://wa.me/2250707000001?text=Bonjour" in html
    assert customer.whatsapp_number not in html and "En attente de paiement" in html
    order.status = OrderStatus.PAID
    await db_session.commit()
    html = (await client.get(f"/c/{code.lower()}")).text
    assert "Paiement bien reçu" in html and "pay.wave.com" not in html
    for bad in ("INCONNU999", "x'><script>", "AB"):
        assert (await client.get(f"/c/{bad}")).status_code == 404


# ---------------------------------------------------------------- Super Admin et tableau de bord

@pytest.mark.asyncio
async def test_superadmin_switch_and_shop_templates(client, db_session, monkeypatch):
    headers = await _admin(client, db_session)
    assert (await client.get("/api/v1/superadmin/wa-templates/settings")).status_code == 401
    d = (await client.get("/api/v1/superadmin/wa-templates/settings", headers=headers)).json()
    assert d["enabled"] is False
    tenant = await _shop(db_session)
    view = (await client.get(f"/api/v1/superadmin/tenants/{tenant.id}/wa-templates", headers=headers)).json()
    assert view["platform_enabled"] is False and view["connected"] is True and len(view["items"]) == 6
    assert [i["used"] for i in view["items"]] == [True, False] * 3  # la boutique vouvoie
    r = await client.post(f"/api/v1/superadmin/tenants/{tenant.id}/wa-templates/sync", headers=headers)
    assert r.json()["report"]["error"].startswith("Les modèles WhatsApp sont éteints")

    d = (await client.put("/api/v1/superadmin/wa-templates/settings", headers=headers, json={"enabled": True})).json()
    assert d["enabled"] is True
    meta = FakeMeta()
    monkeypatch.setattr("app.integrations.whatsapp.client.MetaTemplatesClient", lambda waba, token: meta)
    r = (await client.post(f"/api/v1/superadmin/tenants/{tenant.id}/wa-templates/sync", headers=headers)).json()
    assert r["report"]["created"] == 6 and {i["status"] for i in r["items"]} == {"PENDING"}
    assert {i["status_label"] for i in r["items"]} == {"En attente de Meta"}
    d = (await client.get("/api/v1/superadmin/wa-templates/settings", headers=headers)).json()
    assert d["shops"] == 1 and d["pending"] == 6

    r = (await client.put(f"/api/v1/superadmin/tenants/{tenant.id}/wa-templates/allowed", headers=headers, json={"allowed": False})).json()
    assert r["allowed"] is False
    history = (await client.get(f"/api/v1/superadmin/tenants/{tenant.id}/history", headers=headers)).json()["items"]
    assert sorted(h["what"] for h in history) == ["Modèles WhatsApp créés / vérifiés chez Meta", "Modèles WhatsApp désactivés pour la boutique"]
    actions = (await db_session.execute(select(AuditLog.action).where(AuditLog.action.like("WA_TEMPLATES_%")))).scalars().all()
    assert sorted(actions) == ["WA_TEMPLATES_BLOCKED", "WA_TEMPLATES_ON", "WA_TEMPLATES_SYNCED"]


@pytest.mark.asyncio
async def test_tenant_view_and_connection_hook(client, db_session, monkeypatch):
    tenant = await _shop(db_session, address_form="TU")
    headers = await _headers(client, tenant)
    d = (await client.get("/api/v1/whatsapp/templates", headers=headers)).json()
    assert d["platform_enabled"] is False and d["form"] == "TU"
    assert [i["name"] for i in d["items"] if i["used"]] == ["bob_commande_paiement_tu", "bob_livraison_expediee_tu", "bob_livraison_livree_tu"]

    from app.api.whatsapp import routes as wa_routes
    from app.integrations.whatsapp.dependency import get_meta_oauth_client
    from app.main import app

    class OAuth:
        async def exchange_code_for_token(self, code):
            return "tok"

        async def subscribe_app_to_waba(self, waba_id, token):
            return None
    scheduled = []

    async def fake_sync(tenant_id):
        scheduled.append(tenant_id)
    monkeypatch.setattr(wt, "sync_in_background", fake_sync)
    app.dependency_overrides[get_meta_oauth_client] = lambda: OAuth()
    try:
        for on in (False, True):
            other = await _cabinet(db_session, business_type=ONLINE_STORE, with_account=False)
            await _on(db_session, on)
            h = await _headers(client, other)
            r = await client.post("/api/v1/whatsapp/embedded-signup/callback", headers=h,
                                  json={"code": "c", "waba_id": f"w{on}", "phone_number_id": f"pn-{uuid.uuid4().hex[:6]}"})
            assert r.status_code == 200, r.text
        assert scheduled == [other.id]  # seulement quand l'interrupteur est allumé
    finally:
        app.dependency_overrides.pop(get_meta_oauth_client, None)
    assert wa_routes  # module chargé


def test_pages():
    assert 'id="wa-templates-card"' in DASH and "Messages WhatsApp après 20 h" in DASH and "loadWaTemplates();" in DASH
    assert 'IN_TRANSIT: "En route"' in DASH and 'id="delivery-notify-${orderId}" checked' in DASH and "notify_customer:" in DASH
    assert 'id="wa-switch-card"' in SA and 'id="shop-wa"' in SA and "Créer / vérifier les modèles" in SA
    assert "Pensez à désactiver les modèles sur la boutique du compte d'examen Meta." in SA
    for forbidden in ("alert(", "confirm(", "prompt("):
        assert not re.search(r"(?<![\w.])" + re.escape(forbidden), SA.replace("saConfirm(", ""))


def test_scheduled_refresh_is_declared():
    from app.services.monitoring import TASK_LABELS
    from app.workers.celery_app import celery_app

    tasks = {e["task"] for e in celery_app.conf.beat_schedule.values()}
    assert "app.workers.wa_templates.refresh_templates_task" in tasks and "app.workers.wa_templates.refresh_templates_task" in TASK_LABELS
