"""
Lot 80 — alertes « nouveau bien » et bailleurs en DPE F ou G (décisions du 17/10).

Rapprochement par règles fixes ; seulement aux contacts qui ont accepté ; WhatsApp gratuit dans les 20 h, sinon
email ; modèle WhatsApp payant : plan Pro seulement ; plafonds Indépendant 30 et Starter 50 par mois ; jamais deux
fois le même bien au même prix ; 2 alertes par contact et par semaine ; projet sans nouvelle depuis 90 jours ignoré.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.product import Product
from app.models.property_alert import PropertyAlert
from app.models.prospect_profile import ProspectProfile
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import product_sheets, property_alerts, real_estate

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)  # lundi, 12 h à Paris


async def _agency(db, plan=TenantPlan.STARTER, **fields):
    email = f"a{uuid.uuid4().hex[:8]}@l80.fr"
    tenant = Tenant(name="Agence du Parc", country="FR", currency="EUR", email=email, plan=plan,
                    business_type="REAL_ESTATE", business_type_chosen_at=NOW, **fields)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    db.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=f"pn-{uuid.uuid4().hex[:6]}",
                           system_user_token="t", display_phone_number="+33 6 12 34 56 78"))
    await db.commit()
    return tenant


async def _contact(db, tenant, projects, consent=True, email=None, wrote_hours_ago=None, name="Claire"):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"336{uuid.uuid4().int % 10**8:08d}", first_name=name,
                        email=email, marketing_consent=consent)
    db.add(customer)
    await db.flush()
    stamp = NOW.isoformat()
    db.add(ProspectProfile(tenant_id=tenant.id, customer_id=customer.id,
                           real_estate={"projects": {k: {"updated_at": stamp, **v} for k, v in projects.items()}}))
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.flush()
    if wrote_hours_ago is not None:
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                       content="Bonjour", created_at=NOW - timedelta(hours=wrote_hours_ago)))
    await db.commit()
    return customer


def _bien(tenant, name="T3 lumineux avec balcon", price=1150, code="APPARTEMENT", stock=1, **sheet):
    sheet = {"transaction": "Location", "city": "Lyon 3e", "rooms": 3, "surface": 68, **sheet}
    return Product(tenant_id=tenant.id, sku=f"B{uuid.uuid4().hex[:6]}", name=name, price=price, currency="EUR",
                   stock_quantity=stock, active=True, image_url="https://cdn.example.com/t3.jpg", bob_category=code,
                   sheet=product_sheets.normalize_sheet(code, sheet), created_at=NOW - timedelta(days=1))


class Outbox:
    def __init__(self):
        self.emails, self.whatsapp = [], []

    def email(self, **kw):
        self.emails.append(kw)
        return True

    async def wa(self, db, tenant, item, product):
        self.whatsapp.append((item["customer_obj"].first_name, product.name, item["reason"]))
        return True


# --- Règles du rapprochement -------------------------------------------------------------------------------

@pytest.mark.parametrize("text, value", [
    ("300 k", 300000), ("400 000 €", 400000), ("1,2 M", 1200000), ("1 200 €/mois", 1200), ("entre 250 et 300 k", 300000),
    ("850 euros charges comprises", 850), ("400.000", 400000), ("", None), ("je ne sais pas", None),
])
def test_budget_is_read_from_the_client_words(text, value):
    assert property_alerts.budget_value(text) == (Decimal(value) if value is not None else None)


@pytest.mark.parametrize("project, ok", [
    ({"zone": "Lyon", "budget": "1 200 €"}, True),
    ({"zone": "Lyon 3e ou Villeurbanne"}, True),
    ({"zone": "Villeurbanne"}, False),
    ({"budget": "1 100 €"}, True),            # 1 150 ≤ 1 100 × 1,05
    ({"budget": "1 000 €"}, False),
    ({"budget": "1 080 €"}, False),           # 1 150 > 1 080 × 1,05 = 1 134
    ({"zone": "Lyon", "rooms_min": 4}, False),
    ({"zone": "Lyon", "surface_min": 60}, True),
    ({"zone": "Lyon", "property_type": "MAISON"}, False),
    ({"zone": "Lyon", "furnished": "MEUBLE"}, True),   # la fiche ne dit rien : pas d'exclusion
    ({"property_type": "APPARTEMENT", "rooms_min": 2}, False),  # ni secteur ni budget : trop vague
])
def test_matching_rules(project, ok):
    product = Product(name="T3 lumineux", price=1150, bob_category="APPARTEMENT",
                      sheet={"transaction": "Location", "city": "Lyon 3e", "rooms": 3, "surface": 68})
    assert property_alerts.matches(product, "LOCATION", project) is ok
    assert property_alerts.matches(product, "ACHAT", project) is False  # un achat ne reçoit pas un bien à louer


def test_old_projects_are_ignored():
    assert property_alerts.project_active({"updated_at": (NOW - timedelta(days=89)).isoformat()}, None, NOW)
    assert not property_alerts.project_active({"updated_at": (NOW - timedelta(days=91)).isoformat()}, None, NOW)
    assert not property_alerts.project_active({}, NOW - timedelta(days=120), NOW)  # projet du lot 79, sans date
    assert not property_alerts.project_active({"updated_at": "pas une date"}, None, NOW)


@pytest.mark.asyncio
async def test_project_update_is_dated_and_keeps_the_dpe(db_session):
    tenant = await _agency(db_session)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="33611110000")
    db_session.add(customer)
    await db_session.flush()
    current, rejected = await real_estate.update_project(db_session, tenant.id, customer.id,
                                                         {"project": "GESTION", "dpe": "G", "address": "Lyon"})
    assert current["dpe"] == "G" and rejected == [] and datetime.fromisoformat(current["updated_at"])
    _, rejected = await real_estate.update_project(db_session, tenant.id, customer.id, {"project": "ACHAT", "dpe": "C"})
    assert rejected == ["dpe"]
    assert "DPE G" in real_estate._project_line("GESTION", current)


# --- Qui reçoit quoi ----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_candidates_channels_and_reasons(db_session):
    tenant = await _agency(db_session)
    recent = await _contact(db_session, tenant, {"LOCATION": {"zone": "Lyon", "budget": "1 200 €"}}, wrote_hours_ago=2, name="Awa")
    mail = await _contact(db_session, tenant, {"LOCATION": {"zone": "Lyon"}}, email="b@ex.fr", wrote_hours_ago=30, name="Bruno")
    await _contact(db_session, tenant, {"LOCATION": {"zone": "Lyon"}}, consent=False, email="c@ex.fr", name="Chloé")
    await _contact(db_session, tenant, {"LOCATION": {"zone": "Lyon"}}, name="David")
    await _contact(db_session, tenant, {"ACHAT": {"zone": "Lyon"}}, email="e@ex.fr", name="Emma")
    old = await _contact(db_session, tenant, {"LOCATION": {"zone": "Lyon"}}, email="f@ex.fr", name="Fanny")
    profile = (await db_session.execute(select(ProspectProfile).where(ProspectProfile.customer_id == old.id))).scalar_one()
    profile.real_estate = {"projects": {"LOCATION": {"zone": "Lyon", "updated_at": (NOW - timedelta(days=100)).isoformat()}}}
    product = _bien(tenant)
    db_session.add(product)
    await db_session.commit()

    items = {i["customer"]: i for i in await property_alerts.candidates(db_session, tenant, product, NOW)}
    assert set(items) == {"Awa", "Bruno", "Chloé", "David"}  # Emma achète, le projet de Fanny est trop ancien
    assert items["Awa"]["channel"] == "WHATSAPP" and items["Bruno"]["channel"] == "EMAIL"
    assert items["Chloé"]["blocked"] == "n'a pas accepté d'être prévenu"
    assert items["David"]["blocked"] == "pas d'email ni de conversation récente"
    assert [i["customer"] for i in property_alerts.public(list(items.values()))][:2] == ["Awa", "Bruno"]
    # Bien vendu ou loué : personne.
    product.stock_quantity = 0
    assert await property_alerts.candidates(db_session, tenant, product, NOW) == []


@pytest.mark.asyncio
async def test_send_once_then_price_drop_then_weekly_cap(db_session):
    tenant = await _agency(db_session)
    await _contact(db_session, tenant, {"LOCATION": {"zone": "Lyon"}}, wrote_hours_ago=2, name="Awa")
    await _contact(db_session, tenant, {"LOCATION": {"zone": "Lyon"}}, email="b@ex.fr", name="Bruno")
    product = _bien(tenant, fees="12 €/m² à la charge du locataire")
    db_session.add(product)
    await db_session.commit()
    box = Outbox()

    report = await property_alerts.send_for_product(db_session, tenant, product, "agent-1", NOW, box.email, box.wa)
    assert report["sent"] == 2 and report["by_channel"] == {"WHATSAPP": 1, "EMAIL": 1, "TEMPLATE": 0}
    assert report["quota_left"] == 48
    mail = box.emails[0]
    assert mail["to"] == "b@ex.fr" and mail["subject"] == "Un bien pour votre recherche — Agence du Parc"
    assert "Bonjour Bruno, un nouveau bien correspond à votre recherche :" in mail["body"]
    assert "T3 lumineux avec balcon (À louer · 3 pièces · 68 m² · Lyon 3e)" in mail["body"]
    assert "Prix : 1 150 EUR — honoraires : 12 €/m² à la charge du locataire" in mail["body"]
    assert "https://wa.me/33612345678?text=" in mail["body"] and "Se désinscrire" in mail["body"]
    assert "List-Unsubscribe" in mail["extra_headers"]

    again = await property_alerts.send_for_product(db_session, tenant, product, "agent-1", NOW, box.email, box.wa)
    assert again["sent"] == 0  # jamais deux fois le même bien au même prix
    product.price = 1090
    await db_session.commit()
    drop = await property_alerts.send_for_product(db_session, tenant, product, "agent-1", NOW, box.email, box.wa)
    assert drop["sent"] == 2 and box.whatsapp[-1][2] == "PRICE_DROP"
    assert box.emails[-1]["subject"] == "Baisse de prix — Agence du Parc"
    other = _bien(tenant, name="T2 rénové", price=900)
    db_session.add(other)
    await db_session.commit()
    blocked = await property_alerts.candidates(db_session, tenant, other, NOW)
    assert {i["blocked"] for i in blocked} == {"déjà 2 alertes cette semaine"}
    alerts = (await db_session.execute(select(PropertyAlert))).scalars().all()
    assert len(alerts) == 4 and {a.sent_by for a in alerts} == {"agent-1"}


@pytest.mark.asyncio
async def test_no_price_without_fees_and_no_alert_after_a_visit_request(db_session):
    from app.models.appointment_request import AppointmentRequest

    tenant = await _agency(db_session)
    bruno = await _contact(db_session, tenant, {"LOCATION": {"zone": "Lyon"}}, email="b@ex.fr", name="Bruno")
    visitor = await _contact(db_session, tenant, {"LOCATION": {"zone": "Lyon"}}, email="v@ex.fr", name="Vera")
    product = _bien(tenant)  # pas d'honoraires dans la fiche
    db_session.add(product)
    await db_session.flush()
    conversation = (await db_session.execute(select(Conversation).where(Conversation.customer_id == visitor.id))).scalar_one()
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=visitor.id,
                                      product_id=product.id, kind="VISITE", availability="samedi"))
    await db_session.commit()
    items = {i["customer"]: i for i in await property_alerts.candidates(db_session, tenant, product, NOW)}
    assert items["Vera"]["blocked"] == "visite déjà demandée" and items["Bruno"]["blocked"] is None
    box = Outbox()
    await property_alerts.send_for_product(db_session, tenant, product, "a", NOW, box.email, box.wa)
    assert len(box.emails) == 1 and "Prix" not in box.emails[0]["body"]  # loi ALUR : jamais un prix sans honoraires
    assert bruno.id is not None


@pytest.mark.asyncio
async def test_paid_template_row_is_refused_outside_pro(db_session):
    from app.models.preregistration import PlatformSettings
    from app.models.whatsapp_template import WhatsAppTemplate
    from app.services import wa_templates

    settings = await db_session.get(PlatformSettings, 1)
    if settings is None:
        settings = PlatformSettings(id=1)
        db_session.add(settings)
    settings.wa_templates_enabled = True
    found = {}
    for plan in (TenantPlan.STARTER, TenantPlan.PRO):
        tenant = await _agency(db_session, plan=plan)
        db_session.add(WhatsAppTemplate(tenant_id=tenant.id, key="nouveau_bien", form="VOUS", name="bob_nouveau_bien_vous",
                                        status="APPROVED", category="MARKETING"))
        await db_session.commit()
        found[plan] = await wa_templates.approved(db_session, tenant, "nouveau_bien")
    assert found[TenantPlan.STARTER] is None and found[TenantPlan.PRO] is not None


@pytest.mark.parametrize("plan, quota", [(TenantPlan.INDEPENDANT, 30), (TenantPlan.STARTER, 50),
                                         (TenantPlan.PRO, None), (TenantPlan.ENTERPRISE, None)])
def test_plan_quotas(plan, quota):
    tenant = Tenant(plan=plan, business_type="REAL_ESTATE")
    assert property_alerts.available(tenant) and property_alerts.monthly_quota(tenant) == quota
    assert property_alerts.is_pro(tenant) is (quota is None)
    assert not property_alerts.available(Tenant(plan=TenantPlan.FREE, business_type="REAL_ESTATE"))
    assert not property_alerts.available(Tenant(plan=TenantPlan.PRO, business_type="ONLINE_STORE"))


@pytest.mark.asyncio
async def test_monthly_quota_stops_the_sending(db_session):
    tenant = await _agency(db_session, plan=TenantPlan.INDEPENDANT)
    dummy = await _contact(db_session, tenant, {}, name="Ancien")
    seed = _bien(tenant, name="Ancien bien")
    db_session.add(seed)
    await db_session.flush()
    for _ in range(29):
        db_session.add(PropertyAlert(tenant_id=tenant.id, product_id=seed.id, customer_id=dummy.id, channel="EMAIL",
                                     reason="NEW", price=1, sent_by="x", sent_at=NOW - timedelta(days=1)))
    # Le mois précédent ne compte pas.
    db_session.add(PropertyAlert(tenant_id=tenant.id, product_id=seed.id, customer_id=dummy.id, channel="EMAIL",
                                 reason="NEW", price=1, sent_by="x", sent_at=NOW - timedelta(days=40)))
    for name in ("Awa", "Bruno"):
        await _contact(db_session, tenant, {"LOCATION": {"zone": "Lyon"}}, email=f"{name}@ex.fr", name=name)
    product = _bien(tenant)
    db_session.add(product)
    await db_session.commit()
    assert await property_alerts.quota_left(db_session, tenant, NOW) == 1
    box = Outbox()
    report = await property_alerts.send_for_product(db_session, tenant, product, "a", NOW, box.email, box.wa)
    assert report["sent"] == 1 and report["quota_reached"] is True and report["quota_left"] == 0


@pytest.mark.asyncio
async def test_paid_template_only_for_pro(db_session, monkeypatch):
    sent = []

    async def fake_send(db, tenant, customer, conversation, key, values, button=None, message_type=None):
        sent.append((key, values, message_type))
        return True

    async def approved(db, tenant, key):
        return object()

    monkeypatch.setattr("app.services.wa_templates.send", fake_send)
    monkeypatch.setattr("app.services.wa_templates.approved", approved)
    starter = await _agency(db_session, plan=TenantPlan.STARTER, property_alert_templates=True)
    await _contact(db_session, starter, {"LOCATION": {"zone": "Lyon"}}, name="David")
    product = _bien(starter)
    db_session.add(product)
    await db_session.commit()
    [item] = await property_alerts.candidates(db_session, starter, product, NOW)
    assert item["channel"] is None and item["blocked"] == "pas d'email ni de conversation récente"

    pro = await _agency(db_session, plan=TenantPlan.PRO)
    await _contact(db_session, pro, {"LOCATION": {"zone": "Lyon"}}, name="David")
    bien = _bien(pro)
    db_session.add(bien)
    await db_session.commit()
    [item] = await property_alerts.candidates(db_session, pro, bien, NOW)
    assert item["blocked"] == "pas d'email ni de conversation récente (modèle WhatsApp non activé)"
    pro.property_alert_templates = True
    await db_session.commit()
    report = await property_alerts.send_for_product(db_session, pro, bien, "a", NOW, Outbox().email, Outbox().wa)
    assert report["by_channel"]["TEMPLATE"] == 1
    assert sent == [("nouveau_bien", ["David", "Agence du Parc", "T3 lumineux avec balcon (À louer · 3 pièces · 68 m² · Lyon 3e)"],
                     "property_alert")]


@pytest.mark.asyncio
async def test_free_whatsapp_alert_is_kept_in_the_conversation(db_session, monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def send_text_message(self, to, body):
            calls.append(("text", body))

        async def send_product_card(self, to, link, body, button_id, button_title):
            calls.append(("card", body, button_id, button_title))

    monkeypatch.setattr("app.integrations.whatsapp.client.WhatsAppClient", FakeClient)
    tenant = await _agency(db_session)
    customer = await _contact(db_session, tenant, {"LOCATION": {"zone": "Lyon"}}, wrote_hours_ago=1, name="Awa")
    product = _bien(tenant)
    db_session.add(product)
    await db_session.commit()
    report = await property_alerts.send_for_product(db_session, tenant, product, "a", NOW, Outbox().email)
    assert report["by_channel"]["WHATSAPP"] == 1
    assert calls[0] == ("text", "Bonjour Awa, un nouveau bien correspond à votre recherche :")
    assert calls[1] == ("card", "T3 lumineux avec balcon\nÀ louer · 3 pièces · 68 m² · Lyon 3e",
                        f"interest:{product.id}", "Ça m'intéresse")
    trace = (await db_session.execute(select(Message).where(Message.message_type == "property_alert"))).scalar_one()
    assert trace.sender == MessageSender.SYSTEM and trace.message_metadata["product_ids"] == [str(product.id)]
    # Bob relit l'alerte : si le client répond « oui il m'intéresse », il sait de quel bien il parle.
    from app.agents.orchestrator import _history_to_anthropic_messages

    history = _history_to_anthropic_messages([trace])
    assert history == [{"role": "assistant", "content": trace.content}] and "T3 lumineux" in trace.content
    assert customer.id is not None


@pytest.mark.asyncio
async def test_automatic_sending_for_pro(db_session):
    tenant = await _agency(db_session, plan=TenantPlan.PRO, property_alerts_auto=True)
    await _contact(db_session, tenant, {"LOCATION": {"zone": "Lyon"}}, email="b@ex.fr", name="Bruno")
    new = _bien(tenant)
    old = _bien(tenant, name="T3 ancien", price=1100)
    old.created_at = NOW - timedelta(days=30)
    db_session.add_all([new, old])
    await db_session.commit()
    box = Outbox()
    night = NOW.replace(hour=21)  # 23 h à Paris
    assert await property_alerts.run_automatic(db_session, tenant, night, box.email, box.wa) == 0
    assert await property_alerts.run_automatic(db_session, tenant, NOW, box.email, box.wa) == 1
    assert [m["body"] for m in box.emails][0].count("T3 lumineux avec balcon") == 1  # l'ancien bien n'est pas « nouveau »
    starter = await _agency(db_session, plan=TenantPlan.STARTER, property_alerts_auto=True)
    await _contact(db_session, starter, {"LOCATION": {"zone": "Lyon"}}, email="s@ex.fr", name="Sam")
    db_session.add(_bien(starter))
    await db_session.commit()
    assert await property_alerts.run_automatic(db_session, starter, NOW, box.email, box.wa) == 0  # Pro seulement


@pytest.mark.asyncio
async def test_dpe_owners(db_session):
    tenant = await _agency(db_session, plan=TenantPlan.PRO)
    await _contact(db_session, tenant, {"GESTION": {"address": "Lyon 8e", "dpe": "F"}}, name="Fabrice")
    await _contact(db_session, tenant, {"VENTE": {"address": "Bron", "dpe": "G"}}, name="Gilles")
    await _contact(db_session, tenant, {"GESTION": {"address": "Lyon", "dpe": "D"}}, name="Denis")
    owners = await property_alerts.dpe_owners(db_session, tenant)
    assert [(o["customer"], o["dpe"]) for o in owners] == [("Gilles", "G"), ("Fabrice", "F")]
    assert owners[0]["deadline"].startswith("Classe G : ne peut plus être loué") and "1er janvier 2025" in owners[0]["deadline"]
    assert "1er janvier 2028" in owners[1]["deadline"] and owners[1]["conversation_id"]


# --- API et tableau de bord ----------------------------------------------------------------------------------

async def _headers(client, tenant):
    token = (await client.post("/api/v1/auth/login", data={"username": tenant.email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_api_for_a_starter_agency(client, db_session, monkeypatch):
    box = Outbox()
    monkeypatch.setattr("app.services.email_service.send_email", box.email)
    tenant = await _agency(db_session)
    await _contact(db_session, tenant, {"LOCATION": {"zone": "Lyon"}}, email="b@ex.fr", name="Bruno")
    product = _bien(tenant)
    db_session.add(product)
    await db_session.commit()
    headers = await _headers(client, tenant)
    settings = (await client.get("/api/v1/property-alerts/settings", headers=headers)).json()
    assert settings["quota"] == 50 and settings["pro"] is False and settings["auto"] is False
    assert (await client.put("/api/v1/property-alerts/settings", headers=headers, json={"auto": True})).status_code == 403
    assert (await client.get("/api/v1/property-alerts/dpe-owners", headers=headers)).status_code == 403
    counts = (await client.get("/api/v1/property-alerts/counts", headers=headers)).json()
    assert counts == {str(product.id): {"matches": 1, "ready": 1}}
    detail = (await client.get(f"/api/v1/property-alerts/products/{product.id}", headers=headers)).json()
    assert detail["ready"] == 1 and detail["items"][0]["channel_label"] == "Email" and detail["quota_left"] == 50
    sent = (await client.post(f"/api/v1/property-alerts/products/{product.id}/send", headers=headers)).json()
    assert sent["sent"] == 1 and len(box.emails) == 1
    other = await _agency(db_session)
    other_headers = await _headers(client, other)
    assert (await client.get(f"/api/v1/property-alerts/products/{product.id}", headers=other_headers)).status_code == 404


@pytest.mark.asyncio
async def test_api_for_pro_and_free(client, db_session):
    pro = await _agency(db_session, plan=TenantPlan.PRO)
    headers = await _headers(client, pro)
    saved = (await client.put("/api/v1/property-alerts/settings", headers=headers,
                              json={"auto": True, "templates": True})).json()
    assert saved["auto"] is True and saved["templates"] is True and saved["quota"] is None
    assert (await client.get("/api/v1/property-alerts/dpe-owners", headers=headers)).json() == {"items": []}
    free = await _agency(db_session, plan=TenantPlan.FREE)
    free_headers = await _headers(client, free)
    body = (await client.get("/api/v1/property-alerts/settings", headers=free_headers)).json()
    assert body == {"available": False, "message": property_alerts.NOT_AVAILABLE}
    product = _bien(free)
    db_session.add(product)
    await db_session.commit()
    assert (await client.post(f"/api/v1/property-alerts/products/{product.id}/send", headers=free_headers)).status_code == 403
    store = await _agency(db_session, plan=TenantPlan.PRO)
    store.business_type = "ONLINE_STORE"
    await db_session.commit()
    assert (await client.get("/api/v1/property-alerts/settings", headers=await _headers(client, store))).status_code == 403


def test_template_is_marketing_and_pro_only():
    from app.services import wa_templates

    payload = wa_templates.create_payload("nouveau_bien", "VOUS")
    assert payload["category"] == "MARKETING" and "répondez STOP" in payload["components"][0]["text"]
    assert wa_templates.create_payload("rdv_rappel", "VOUS")["category"] == "UTILITY"
    pro = Tenant(plan=TenantPlan.PRO, business_type="REAL_ESTATE")
    starter = Tenant(plan=TenantPlan.STARTER, business_type="REAL_ESTATE")
    assert ("nouveau_bien", "VOUS") in wa_templates.wanted(pro)
    assert ("nouveau_bien", "VOUS") not in wa_templates.wanted(starter)
    assert ("rdv_rappel", "VOUS") in wa_templates.wanted(starter)


def test_worker_dashboard_and_prompt():
    from app.agents.prompts import REAL_ESTATE_RULES
    from app.services.monitoring import TASK_LABELS
    from app.workers.celery_app import TASK_MODULES, celery_app

    assert "app.workers.property_alerts" in TASK_MODULES
    assert celery_app.conf.beat_schedule["property-alerts"]["task"] == "app.workers.property_alerts.run_alerts_task"
    assert "app.workers.property_alerts.run_alerts_task" in TASK_LABELS
    for marker in ('id="alerts-card"', 'id="dpe-card"', "loadPropertyAlerts()", "togglePropertyAlertPanel(",
                   '"/api/v1/property-alerts/settings"', "Envoi automatique (entre 9 h et 20 h)", "alert-link-${p.id}"):
        assert marker in HTML, marker
    assert "la classe DPE" in REAL_ESTATE_RULES
