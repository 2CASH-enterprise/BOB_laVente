"""
Lot 49 — relances par EMAIL (jamais sur WhatsApp), avec l'offre du moment du commerçant ; et règle
unique : tout message WhatsApp qui ne répond pas à un message qui vient d'arriver part seulement
dans les 20 h qui suivent le dernier message du client, sinon par email.
"""
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.customer_product_view import CustomerProductView
from app.models.followup_settings import TenantFollowupSettings
from app.models.order import Order, OrderStatus
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.business_type import CAR_DEALERSHIP
from app.services.followup_service import active_offer, build_followup_email, run_followups_for_tenant
from app.services.human_reply import customer_window_open
from app.tests.test_appointments import _next_saturday_10h_local
from app.tests.test_appointments import _shop as _dealer_shop
from app.tests.test_appointments import whatsapp  # noqa: F401 — fixture

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class Mailbox:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def __call__(self, **mail):
        self.sent.append(mail)
        return self.ok


async def _tenant(db, email, business_type="ONLINE_STORE", address_form="VOUS", number="+221 77 123 45 67"):
    tenant = Tenant(name="Boutique Awa", country="SN", currency="XOF", email=email, plan=TenantPlan.PRO,
                    business_type=business_type)
    if hasattr(tenant, "address_form"):
        tenant.address_form = address_form
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    db.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=f"pn-{uuid.uuid4().hex[:8]}",
                           system_user_token="t", display_phone_number=number))
    settings = TenantFollowupSettings(tenant_id=tenant.id, enabled=True, first_followup_hours=24, second_followup_hours=72)
    db.add(settings)
    await db.commit()
    return tenant, settings


async def _customer(db, tenant, email="client@example.com", consent=True, first_name="Fatou", silent_hours=30):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}", first_name=first_name,
                        email=email, marketing_consent=consent)
    db.add(customer)
    await db.flush()
    conv = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
                        last_message_at=datetime.now(timezone.utc) - timedelta(hours=silent_hours))
    db.add(conv)
    await db.commit()
    return customer, conv


# --- La règle des 20 h, verrouillée dans le code -------------------------------------------------------------

# Chaque envoi WhatsApp du code est listé ici, avec sa justification. Un nouvel envoi fait échouer ce test
# tant qu'on n'a pas vérifié qu'il respecte la règle des 20 h.
SEND_SITES = {
    "api/webhooks/whatsapp.py": 5,      # réponses au message que le client vient d'envoyer (fenêtre ouverte) ; lot 57 : accusé de réception
    "agents/tools.py": 1,               # confirmation de commande pendant la conversation (idem)
    "api/messages/routes.py": 1,        # réponse humaine : ensure_reply_window_open
    "services/appointment_service.py": 1,  # messages fixes : ensure_reply_window_open (puis email)
    "api/orders/routes.py": 2,          # reçu et annulation : customer_window_open (sinon email)
}


def _send_calls():
    found = {}
    for path in (ROOT).rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "integrations/")):
            continue
        count = len(re.findall(r"\.send_(?:text|image|template|interactive|document)_message\(", path.read_text(encoding="utf-8")))
        if count:
            found[rel] = count
    return found


def test_every_whatsapp_send_is_reviewed():
    assert _send_calls() == SEND_SITES


def test_proactive_sends_check_the_20h_window():
    orders = (ROOT / "api/orders/routes.py").read_text(encoding="utf-8")
    for match in re.finditer(r"\.send_text_message\(", orders):
        before = orders[max(0, match.start() - 900):match.start()]
        assert "customer_window_open(" in before
    service = (ROOT / "services/appointment_service.py").read_text(encoding="utf-8")
    fixed = service[service.index("async def send_fixed_message("):service.index("async def notify_customer(")]
    assert fixed.index("ensure_reply_window_open(") < fixed.index(".send_text_message(")
    replies = (ROOT / "api/messages/routes.py").read_text(encoding="utf-8")
    assert replies.index("ensure_reply_window_open(") < replies.index(".send_text_message(")


def test_relances_never_use_whatsapp():
    source = (ROOT / "services/followup_service.py").read_text(encoding="utf-8")
    assert "WhatsAppClient" not in source and "send_text_message" not in source


@pytest.mark.asyncio
async def test_customer_window_is_20_hours_across_conversations(db_session):
    tenant, _ = await _tenant(db_session, "w@l49.sn")
    customer, conv = await _customer(db_session, tenant)
    assert await customer_window_open(db_session, tenant.id, customer.id) is False  # jamais écrit
    other = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.CLOSED)
    db_session.add(other)
    await db_session.flush()
    db_session.add(Message(tenant_id=tenant.id, conversation_id=other.id, sender=MessageSender.CUSTOMER,
                           message_type="text", content="Bonjour", created_at=NOW - timedelta(hours=19)))
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conv.id, sender=MessageSender.AI,
                           message_type="text", content="Bob", created_at=NOW - timedelta(hours=1)))
    await db_session.commit()
    assert await customer_window_open(db_session, tenant.id, customer.id, now=NOW) is True
    assert await customer_window_open(db_session, tenant.id, customer.id, now=NOW + timedelta(hours=1, minutes=1)) is False
    assert await customer_window_open(db_session, uuid.uuid4(), customer.id, now=NOW) is False  # autre boutique
    assert await customer_window_open(db_session, tenant.id, None, now=NOW) is False


# --- Relances par email -------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_relance_goes_by_email_to_consenting_customers_only(db_session):
    tenant, _ = await _tenant(db_session, "r@l49.sn")
    ok, ok_conv = await _customer(db_session, tenant)
    _, no_consent = await _customer(db_session, tenant, email="b@example.com", consent=False)
    _, no_email = await _customer(db_session, tenant, email=None)
    mailbox = Mailbox()

    sent = await run_followups_for_tenant(db_session, tenant.id, send_email=mailbox)

    assert sent == 1 and [m["to"] for m in mailbox.sent] == ["client@example.com"]
    for conv in (ok_conv, no_consent, no_email):
        await db_session.refresh(conv)
        assert conv.followup_stage == 1  # jamais retentée en boucle
    traced = (await db_session.execute(select(Message).where(Message.message_type == "followup_email"))).scalars().all()
    assert [m.conversation_id for m in traced] == [ok_conv.id]
    assert traced[0].sender == MessageSender.SYSTEM and "Relance envoyée par email" in traced[0].content


@pytest.mark.asyncio
async def test_failed_email_is_not_traced_as_sent(db_session):
    tenant, _ = await _tenant(db_session, "f@l49.sn")
    _, conv = await _customer(db_session, tenant)
    assert await run_followups_for_tenant(db_session, tenant.id, send_email=Mailbox(ok=False)) == 0
    assert (await db_session.execute(select(Message).where(Message.message_type == "followup_email"))).first() is None
    await db_session.refresh(conv)
    assert conv.followup_stage == 1


@pytest.mark.asyncio
async def test_email_is_personal_and_carries_the_offer(db_session):
    tenant, settings = await _tenant(db_session, "p@l49.sn")
    customer, _ = await _customer(db_session, tenant)
    product = Product(tenant_id=tenant.id, sku="ROBE", name="Robe wax", price=15000, currency="XOF", stock_quantity=3)
    db_session.add(product)
    await db_session.flush()
    db_session.add(CustomerProductView(tenant_id=tenant.id, customer_id=customer.id, product_id=product.id))
    settings.offer_text, settings.offer_code, settings.offer_ends_on = "-10 % sur toute la boutique", "RENTREE10", date(2026, 10, 31)
    await db_session.commit()

    mail = await build_followup_email(db_session, tenant, customer, settings, 0, NOW)

    assert mail["to"] == "client@example.com" and mail["from_name"] == "Boutique Awa" and mail["reply_to"] == "p@l49.sn"
    assert mail["subject"] == "Fatou, une offre pour vous chez Boutique Awa"
    body = mail["body"]
    assert body.startswith("Bonjour Fatou,") and settings.first_message in body
    assert "Vous regardiez l'article « Robe wax »" in body
    assert "Offre du moment : -10 % sur toute la boutique\nCode promo : RENTREE10\nValable jusqu'au : 31/10/2026" in body
    assert "Reprendre sur WhatsApp : https://wa.me/221771234567?text=" in body
    assert body.index("Reprendre sur WhatsApp") < body.index("\n\n—")  # bouton avant le pied de page
    assert "Se désinscrire : " in body and "/unsubscribe/" in body
    assert mail["extra_headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


@pytest.mark.asyncio
async def test_expired_or_empty_offer_is_never_mentioned(db_session):
    tenant, settings = await _tenant(db_session, "e@l49.sn")
    customer, _ = await _customer(db_session, tenant)
    settings.offer_text, settings.offer_ends_on = "Soldes d'été", date(2026, 9, 30)
    mail = await build_followup_email(db_session, tenant, customer, settings, 0, NOW)
    assert "Offre du moment" not in mail["body"] and mail["subject"] == "Fatou, on reprend notre échange ?"
    settings.offer_text = "   "
    assert active_offer(settings, NOW.date()) is None
    settings.offer_text, settings.offer_ends_on = "Jeu concours", NOW.date()  # dernier jour : encore valable
    assert active_offer(settings, NOW.date())["text"] == "Jeu concours"


@pytest.mark.asyncio
async def test_second_email_and_without_first_name_or_whatsapp_number(db_session):
    tenant, settings = await _tenant(db_session, "s@l49.sn", number=None)
    customer, _ = await _customer(db_session, tenant, first_name=None)
    mail = await build_followup_email(db_session, tenant, customer, settings, 1, NOW)
    assert mail["subject"] == "Boutique Awa vous attend toujours"
    assert mail["body"].startswith("Bonjour,\n") and settings.second_message in mail["body"]
    assert "wa.me" not in mail["body"]


@pytest.mark.asyncio
async def test_dealership_email_speaks_vehicle(db_session):
    tenant, settings = await _tenant(db_session, "d@l49.sn", business_type=CAR_DEALERSHIP)
    customer, _ = await _customer(db_session, tenant)
    mail = await build_followup_email(db_session, tenant, customer, settings, 0, NOW, viewed="Peugeot 3008")
    assert "Vous regardiez le véhicule « Peugeot 3008 »" in mail["body"]


@pytest.mark.asyncio
async def test_other_shop_views_are_never_used(db_session):
    tenant, settings = await _tenant(db_session, "i1@l49.sn")
    other, _ = await _tenant(db_session, "i2@l49.sn")
    customer, _ = await _customer(db_session, tenant)
    foreign = Product(tenant_id=other.id, sku="X", name="Produit d'une autre boutique", price=1, currency="XOF", stock_quantity=1)
    db_session.add(foreign)
    await db_session.flush()
    db_session.add(CustomerProductView(tenant_id=other.id, customer_id=customer.id, product_id=foreign.id))
    await db_session.commit()
    mail = await build_followup_email(db_session, tenant, customer, settings, 0, NOW)
    assert "autre boutique" not in mail["body"]


# --- Réglages ------------------------------------------------------------------------------------------------------

async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_settings_save_the_offer_and_count_recipients(client, db_session):
    tenant, _ = await _tenant(db_session, "api@l49.sn")
    await _customer(db_session, tenant)
    await _customer(db_session, tenant, email="x@example.com", consent=False)
    other, _ = await _tenant(db_session, "api2@l49.sn")
    await _customer(db_session, other, email="y@example.com")
    headers = await _headers(client, "api@l49.sn")

    r = await client.put("/api/v1/tenants/me/followup-settings", headers=headers, json={
        "offer_text": "  Jeu : gagnez un bon d'achat  ", "offer_code": "", "offer_ends_on": "2026-10-31"})
    assert r.status_code == 200
    body = r.json()
    assert body["offer_text"] == "Jeu : gagnez un bon d'achat" and body["offer_code"] is None
    assert body["offer_ends_on"] == "2026-10-31" and body["eligible_recipients"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"offer_text": "x" * 501}, {"offer_code": "x" * 41}, {"first_followup_hours": 0},
    {"second_followup_hours": 721}, {"first_message": "   "},
])
async def test_settings_are_validated(client, db_session, payload):
    await _tenant(db_session, f"v{uuid.uuid4().hex[:6]}@l49.sn")
    email = (await db_session.execute(select(Tenant.email).order_by(Tenant.created_at.desc()))).scalars().first()
    r = await client.put("/api/v1/tenants/me/followup-settings", headers=await _headers(client, email), json=payload)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_preview_sends_nothing(client, db_session, monkeypatch):
    tenant, settings = await _tenant(db_session, "pv@l49.sn")
    settings.offer_text = "Soldes -20 %"
    await db_session.commit()
    sent = []
    monkeypatch.setattr("app.services.email_service.send_email", lambda **kw: sent.append(kw) or True)

    r = await client.get("/api/v1/tenants/me/followup-preview?stage=2", headers=await _headers(client, "pv@l49.sn"))

    assert r.status_code == 200 and sent == []
    assert r.json()["subject"] == "Awa, une offre pour vous chez Boutique Awa"
    assert "Offre du moment : Soldes -20 %" in r.json()["body"] and "Bonjour Awa," in r.json()["body"]


# --- Reçu et annulation de commande : WhatsApp dans les 20 h, sinon email ------------------------------------------

@pytest.fixture
def order_whatsapp(monkeypatch):
    sent = []

    class _Client:
        def __init__(self, **kw):
            pass

        async def send_text_message(self, to, body):
            sent.append(body)
            return {}

    monkeypatch.setattr("app.api.orders.routes.WhatsAppClient", _Client)
    return sent


async def _order_case(db, email, customer_wrote_hours_ago, customer_email="client@example.com"):
    tenant, _ = await _tenant(db, email)
    customer, conv = await _customer(db, tenant, email=customer_email)
    if customer_wrote_hours_ago is not None:
        db.add(Message(tenant_id=tenant.id, conversation_id=conv.id, sender=MessageSender.CUSTOMER, message_type="text",
                       content="Bonjour", created_at=datetime.now(timezone.utc) - timedelta(hours=customer_wrote_hours_ago)))
    order = Order(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conv.id, status=OrderStatus.PENDING,
                  total_amount=15000, currency="XOF")
    db.add(order)
    await db.commit()
    return tenant, order


@pytest.mark.asyncio
async def test_cancellation_after_20h_goes_by_email(client, db_session, order_whatsapp, monkeypatch):
    mails = Mailbox()
    monkeypatch.setattr("app.services.email_service.send_email", mails)
    _, order = await _order_case(db_session, "c1@l49.sn", customer_wrote_hours_ago=21)

    r = await client.put(f"/api/v1/orders/{order.id}/cancel", json={"notify_customer": True},
                         headers=await _headers(client, "c1@l49.sn"))

    assert r.json()["customer_notified"] is True and r.json()["notified_by"] == "EMAIL"
    assert order_whatsapp == [] and mails.sent[0]["to"] == "client@example.com"
    assert "annulée" in mails.sent[0]["body"] and mails.sent[0]["subject"] == "Commande annulée — Boutique Awa"
    assert (await db_session.execute(select(Message).where(Message.message_type == "order_cancelled"))).first() is None


@pytest.mark.asyncio
async def test_cancellation_within_20h_goes_by_whatsapp(client, db_session, order_whatsapp, monkeypatch):
    mails = Mailbox()
    monkeypatch.setattr("app.services.email_service.send_email", mails)
    _, order = await _order_case(db_session, "c2@l49.sn", customer_wrote_hours_ago=19)

    r = await client.put(f"/api/v1/orders/{order.id}/cancel", json={"notify_customer": True},
                         headers=await _headers(client, "c2@l49.sn"))

    assert r.json()["notified_by"] == "WHATSAPP" and len(order_whatsapp) == 1 and mails.sent == []


@pytest.mark.asyncio
async def test_cancellation_after_20h_without_email(client, db_session, order_whatsapp):
    _, order = await _order_case(db_session, "c3@l49.sn", customer_wrote_hours_ago=None, customer_email=None)
    r = await client.put(f"/api/v1/orders/{order.id}/cancel", json={"notify_customer": True},
                         headers=await _headers(client, "c3@l49.sn"))
    assert r.json()["customer_notified"] is False and r.json()["notified_by"] is None and order_whatsapp == []


@pytest.mark.asyncio
async def test_receipt_after_20h_is_not_sent_on_whatsapp(client, db_session, order_whatsapp, monkeypatch):
    monkeypatch.setattr("app.services.email_service.send_email", Mailbox())
    _, order = await _order_case(db_session, "p1@l49.sn", customer_wrote_hours_ago=25)

    r = await client.put(f"/api/v1/orders/{order.id}/mark-paid", headers=await _headers(client, "p1@l49.sn"))

    assert r.status_code == 200 and order_whatsapp == []
    assert (await db_session.execute(select(Message).where(Message.message_type == "receipt"))).first() is None


@pytest.mark.asyncio
async def test_receipt_within_20h_is_sent_on_whatsapp(client, db_session, order_whatsapp, monkeypatch):
    monkeypatch.setattr("app.services.email_service.send_email", Mailbox())
    _, order = await _order_case(db_session, "p2@l49.sn", customer_wrote_hours_ago=2)

    await client.put(f"/api/v1/orders/{order.id}/mark-paid", headers=await _headers(client, "p2@l49.sn"))

    assert len(order_whatsapp) == 1 and "Reçu" in order_whatsapp[0]
    assert (await db_session.execute(select(Message).where(Message.message_type == "receipt"))).scalar_one()


# --- Rendez-vous (concession) : WhatsApp dans les 20 h, sinon email ----------------------------------------------

@pytest.mark.asyncio
async def test_appointment_confirmation_after_20h_goes_by_email(client, db_session, whatsapp, monkeypatch):  # noqa: F811
    mails = Mailbox()
    monkeypatch.setattr("app.services.email_service.send_email", mails)
    _, _, customer, appt = await _dealer_shop(db_session, "a1@l49.fr", customer_age=timedelta(hours=21))
    customer.email = "awa@example.com"
    await db_session.commit()

    r = await client.post(f"/api/v1/appointments/{appt.id}/confirm",
                          json={"scheduled_local": _next_saturday_10h_local(), "notify_customer": True},
                          headers=await _headers(client, "a1@l49.fr"))

    body = r.json()
    assert body["customer_notified"] is True and body["notified_by"] == "EMAIL" and whatsapp.sent == []
    assert body["appointment"]["can_notify"] is False and body["appointment"]["can_email"] is True
    assert mails.sent[0]["to"] == "awa@example.com" and "est confirmé" in mails.sent[0]["body"]
    assert mails.sent[0]["subject"] == "Votre rendez-vous est confirmé — Auto Plus"


@pytest.mark.asyncio
async def test_appointment_cancellation_within_20h_goes_by_whatsapp(client, db_session, whatsapp, monkeypatch):  # noqa: F811
    mails = Mailbox()
    monkeypatch.setattr("app.services.email_service.send_email", mails)
    _, _, customer, appt = await _dealer_shop(db_session, "a2@l49.fr")
    customer.email = "awa@example.com"
    await db_session.commit()

    r = await client.post(f"/api/v1/appointments/{appt.id}/cancel", json={"notify_customer": True},
                          headers=await _headers(client, "a2@l49.fr"))

    assert r.json()["notified_by"] == "WHATSAPP" and len(whatsapp.sent) == 1 and mails.sent == []


@pytest.mark.asyncio
async def test_appointment_after_20h_without_email_says_so(client, db_session, whatsapp):  # noqa: F811
    _, _, _, appt = await _dealer_shop(db_session, "a3@l49.fr", customer_age=timedelta(hours=21))
    r = await client.post(f"/api/v1/appointments/{appt.id}/confirm",
                          json={"scheduled_local": _next_saturday_10h_local(), "notify_customer": True},
                          headers=await _headers(client, "a3@l49.fr"))
    assert r.json()["customer_notified"] is False and "n'a pas donné d'email" in r.json()["notify_error"]


# --- Tableau de bord ----------------------------------------------------------------------------------------------

def test_dashboard_relances_card():
    card = HTML[HTML.index("Relances par email<button"):HTML.index('onclick="saveFollowupSettings()"')]
    for text in ("Les relances ne partent jamais sur WhatsApp", "accepté de recevoir vos offres",
                 'id="followup-offer-text"', 'id="followup-offer-code"', 'id="followup-offer-ends"',
                 "n'invente jamais d'offre", "previewFollowup(1)", "previewFollowup(2)"):
        assert text in card, text
    js = HTML[HTML.index("async function loadFollowupSettings() {"):HTML.index("// ---- Lot 38 : tutoiement")]
    assert "eligible_recipients" in js and "offer_ends_on" in js and "followup-preview?stage=" in js and "bobAlert(" in js
    assert "Commercial autorisé" not in HTML and "doit être activé sur votre compte" not in HTML


def test_dashboard_says_which_channel_was_used():
    assert 'result.notified_by === "EMAIL"' in HTML and 'r.notified_by === "EMAIL"' in HTML
    assert "a.can_email" in HTML and "sinon par email" in HTML
    assert "plus de 24 h" not in HTML  # la règle de Bob est 20 h


@pytest.mark.asyncio
async def test_last_viewed_product_and_offer_without_code(db_session):
    tenant, settings = await _tenant(db_session, "lv@l49.sn")
    customer, _ = await _customer(db_session, tenant)
    old = Product(tenant_id=tenant.id, sku="A", name="Ancien sac", price=1, currency="XOF", stock_quantity=1)
    new = Product(tenant_id=tenant.id, sku="B", name="Nouvelle robe", price=1, currency="XOF", stock_quantity=1)
    db_session.add_all([old, new])
    await db_session.flush()
    db_session.add_all([
        CustomerProductView(tenant_id=tenant.id, customer_id=customer.id, product_id=new.id, last_viewed_at=NOW - timedelta(hours=1)),
        CustomerProductView(tenant_id=tenant.id, customer_id=customer.id, product_id=old.id, last_viewed_at=NOW - timedelta(days=5)),
    ])
    settings.offer_text = "Livraison offerte ce week-end"
    await db_session.commit()

    body = (await build_followup_email(db_session, tenant, customer, settings, 0, NOW))["body"]

    assert "« Nouvelle robe »" in body and "Ancien sac" not in body
    assert "Offre du moment : Livraison offerte ce week-end" in body and "Code promo" not in body and "Valable jusqu'au" not in body


def test_migration_replaces_only_the_old_default_texts():
    import importlib.util

    from app.models.followup_settings import DEFAULT_FIRST_MESSAGE, DEFAULT_SECOND_MESSAGE

    path = ROOT.parent / "alembic" / "versions" / "e49c2a7d5b18_lot_49_relances_par_email.py"
    spec = importlib.util.spec_from_file_location("lot49_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (module.NEW_FIRST, module.NEW_SECOND) == (DEFAULT_FIRST_MESSAGE, DEFAULT_SECOND_MESSAGE)
    assert module.OLD_FIRST.startswith("Bonjour") and "WHERE {column} = :old" in path.read_text(encoding="utf-8")
