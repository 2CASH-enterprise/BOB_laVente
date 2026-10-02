"""Lot 40 — emails redessinés (HTML + texte), récapitulatif de commande et reçu par email."""
import uuid
from datetime import datetime, timedelta, timezone
from email import message_from_string

import pytest
from sqlalchemy import select

from app.agents.dependency import get_llm_client
from app.core.security import hash_password
from app.main import app
from app.models.appointment_request import AppointmentRequest
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import order_emails
from app.services.business_type import CAR_DEALERSHIP, ONLINE_STORE
from app.services.email_layout import POWERED_BY, render_html
from app.services.email_service import build_message
from app.services.handoff_service import build_handoff_alert, build_outage_alert
from app.services.password_reset_service import build_reset_email
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response


def _parts(msg):
    parsed = message_from_string(msg.as_string())
    parts = {p.get_content_type(): p.get_payload(decode=True).decode("utf-8") for p in parsed.walk() if not p.is_multipart()}
    return parsed, parts


# --- Mise en page ----------------------------------------------------------------------------------

def test_every_email_has_text_and_html():
    parsed, parts = _parts(build_message("a@example.com", "Sujet", "Bonjour,\n\nCorps"))
    assert parsed.get_content_type() == "multipart/alternative"
    assert parts["text/plain"] == "Bonjour,\n\nCorps"  # la version texte reste intacte
    assert "<!DOCTYPE html>" in parts["text/html"] and "Corps" in parts["text/html"]


def test_merchant_alert_has_button_details_and_escaped_values():
    customer = Customer(tenant_id=uuid.uuid4(), whatsapp_number="221700000001", first_name='<script>alert("x")</script>')
    conversation = Conversation(id=uuid.uuid4(), tenant_id=customer.tenant_id, customer_id=uuid.uuid4())
    subject, body = build_handoff_alert(customer, conversation, "Demande <b>remise</b>")
    html = render_html(subject, body, "Bob", for_customer=False)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<b>remise</b>" not in html and "&lt;b&gt;remise&lt;/b&gt;" in html
    button = html.split(f'href="https://agenc-ai.com/bob/dashboard/?conversation={conversation.id}"', 1)[1].split("</a>", 1)[0]
    assert button.endswith(">Ouvrir la conversation") and "background" not in button  # le bouton (fond sur la cellule)
    assert ">Client</td>" in html and ">Raison</td>" in html  # petit tableau
    assert "/dashboard/icon-192.png" in html and ">Bob</span>" in html  # en-tête Bob


def test_only_http_links_become_clickable():
    html = render_html("S", "Lien : javascript:alert(1)\n\nVoir https://example.com/x.", "Bob", False)
    assert 'href="javascript' not in html
    assert 'href="https://example.com/x"' in html  # le point final ne fait pas partie du lien


def test_login_code_is_shown_big():
    html = render_html("Votre code de connexion Bob", "Bonjour,\n\nVoici votre code :\n\n482913\n\nIl expire…", "Bob", False)
    assert "font-size: 34px" in html and ">482913</p>" in html


def test_bullets_and_footer():
    body = "Bonjour,\n\nCe que Bob a appris :\n- Budget : 5 M\n- Reprise : Clio\n\n—\nSe désinscrire : https://x.example/u"
    html = render_html("S", body, "Garage Awa", for_customer=True)
    assert "<ul" in html and "<li" in html and "Budget : 5 M" in html
    assert 'href="https://x.example/u"' in html and "border-radius: 10px; background" not in html.split("<ul")[1]
    assert ">Garage Awa</span>" in html and "icon-192.png" not in html  # email de la boutique : pas d'en-tête Bob


def test_preheader_never_shows_a_link():
    html = render_html("S", "Bonjour,\n\nTexte court.\n\nOuvrir : https://secret.example/token=abc", "Bob", False)
    preheader = html.split('opacity: 0;">', 1)[1].split("</div>", 1)[0]
    assert "https" not in preheader and "Texte court." in preheader


def test_dark_mode_and_mobile_rules():
    html = render_html("S", "Bonjour", "Bob", False)
    assert 'name="color-scheme" content="light dark"' in html and "prefers-color-scheme: dark" in html
    assert "max-width: 620px" in html


# --- Textes mis à jour ------------------------------------------------------------------------------

def test_outage_alert_points_to_the_right_screen():
    _, body = build_outage_alert("Boutique Awa")
    assert "Réglages de Bob → Transmission à un humain" in body and "Paramètres →" not in body


def test_reset_email_has_a_button():
    subject, body = build_reset_email("Awa", "https://agenc-ai.com/bob/dashboard/?reset_token=abc")
    html = render_html(subject, body, "Bob", False)
    assert ">Choisir un nouveau mot de passe</a>" in html


@pytest.mark.asyncio
async def test_login_code_email(client, db_session, unique_email, monkeypatch):
    sent = []
    monkeypatch.setattr("app.api.auth.routes.send_email", lambda **kw: sent.append(kw) or True)
    tenant = Tenant(name="B", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=unique_email, hashed_password=hash_password("x"), full_name="U",
                        role=Role.OWNER, mfa_enabled=True))
    await db_session.commit()
    await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    [mail] = sent
    code_lines = [line for line in mail["body"].split("\n") if line.isdigit()]
    assert len(code_lines) == 1 and len(code_lines[0]) == 6  # le code seul sur sa ligne → affiché en grand


# --- Emails au client : tu/vous et « Propulsé par Bob » ------------------------------------------------

def _tenant(form="VOUS", paid=True, business_type=ONLINE_STORE, payment_link=None):
    return Tenant(id=uuid.uuid4(), name="Boutique Awa", country="SN", currency="XOF", email="awa@example.com",
                  address_form=form, plan=TenantPlan.PRO if paid else TenantPlan.FREE, business_type=business_type,
                  payment_link=payment_link)


def _order(status=OrderStatus.PENDING):
    return Order(id=uuid.uuid4(), tenant_id=uuid.uuid4(), customer_id=uuid.uuid4(), status=status, total_amount=25000,
                 currency="XOF", paid_at=datetime(2026, 10, 2, tzinfo=timezone.utc))


def test_campaign_footer_follows_tu_vous_and_plan():
    from app.services.campaign_service import _build_email_body

    tu = _build_email_body("Promo !", None, "Boutique Awa", "https://x/u", tu=True, powered_by=True)
    vous = _build_email_body("Promo !", None, "Boutique Awa", "https://x/u")
    assert "Tu reçois cet email car tu as accepté" in tu and POWERED_BY in tu
    assert "Vous recevez cet email car vous avez accepté" in vous and POWERED_BY not in vous
    assert "Se désinscrire : https://x/u" in tu and "Se désinscrire : https://x/u" in vous


def test_recap_and_receipt_texts():
    order = _order()
    subject, body = order_emails.recap_email(order, ["Robe wax x1 — 25 000 XOF"], _tenant("TU", paid=False, payment_link="https://pay.example/x"))
    assert subject.startswith("Ta commande") and "Merci pour ta commande" in body and "vous" not in body.lower()
    assert "Cet email t'est envoyé par Boutique Awa." in body
    assert "Payer ma commande : https://pay.example/x" in body and POWERED_BY in body
    assert "- Robe wax x1 — 25 000 XOF" in body and "Total : 25 000 XOF" in body
    subject, body = order_emails.recap_email(order, ["Robe x1"], _tenant("VOUS"))
    assert subject.startswith("Votre commande") and "Payer ma commande" not in body and POWERED_BY not in body
    paid = _order(OrderStatus.PAID)
    _, body = order_emails.recap_email(paid, ["Robe x1"], _tenant("VOUS", payment_link="https://pay.example/x"))
    assert "Payer ma commande" not in body and "Statut : payée" in body
    subject, body = order_emails.receipt_email(paid, ["Robe x1"], _tenant("TU"))
    assert subject.startswith("Reçu de ta commande") and "ton paiement" in body and "Total payé : 25 000 XOF" in body


def test_dealership_customer_emails_carry_powered_by_on_free_plan():
    from app.services.appointment_outcome import email_text
    from app.services.appointment_service import customer_reminder_email
    from zoneinfo import ZoneInfo

    appt = AppointmentRequest(kind="ESSAI", availability="x", scheduled_at=datetime(2026, 10, 3, 10, tzinfo=timezone.utc),
                              outcome="FOLLOW_UP", vehicle_label="Peugeot 3008")
    _, body = customer_reminder_email(appt, "Garage Awa", ZoneInfo("Africa/Dakar"), powered_by=True)
    assert POWERED_BY in body and "Rendez-vous : " in body and "Date : " in body
    _, body = customer_reminder_email(appt, "Garage Awa", ZoneInfo("Africa/Dakar"))
    assert POWERED_BY not in body
    assert POWERED_BY in email_text(appt, "Garage Awa", powered_by=True)[1]
    assert POWERED_BY not in email_text(appt, "Garage Awa")[1]


# --- Récapitulatif réellement envoyé ---------------------------------------------------------------------

async def _store(db, form="VOUS", business_type=ONLINE_STORE, pn=None):
    email = f"s{uuid.uuid4().hex[:6]}@example.com"
    tenant = Tenant(name="Boutique Awa", country="SN", currency="XOF", email=email, plan=TenantPlan.PRO,
                    business_type=business_type, address_form=form)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="Awa", role=Role.OWNER))
    if pn:
        db.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=pn, system_user_token="t"))
    product = Product(tenant_id=tenant.id, sku=f"R{uuid.uuid4().hex[:4]}", name="Robe wax", price=25000, currency="XOF", stock_quantity=5)
    db.add(product)
    await db.commit()
    return tenant, product, email


async def _customer_with_order(db, tenant, product, number, status=OrderStatus.PENDING, age_days=0, email=None):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=number, email=email)
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.flush()
    order = Order(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id, status=status,
                  total_amount=25000, currency="XOF", created_at=datetime.now(timezone.utc) - timedelta(days=age_days))
    db.add(order)
    await db.flush()
    db.add(OrderItem(order_id=order.id, product_id=product.id, quantity=1, unit_price=25000, subtotal=25000))
    await db.commit()
    return customer, order


def _payload(pn, number, text):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": pn},
        "messages": [{"from": number, "id": f"wamid.{uuid.uuid4().hex}", "type": "text", "text": {"body": text}, "timestamp": "1"}],
    }}]}]}


async def _write(client, pn, number, text, llm=None):
    app.dependency_overrides[get_llm_client] = lambda: llm or FakeLLMClient([text_response("Merci !")])
    try:
        return await client.post("/webhooks/whatsapp", json=_payload(pn, number, text))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    fake = lambda **kw: sent.append(kw) or True  # noqa: E731
    monkeypatch.setattr("app.api.webhooks.whatsapp.send_email", fake)
    monkeypatch.setattr("app.api.orders.routes.send_email", fake)
    return sent


@pytest.mark.asyncio
async def test_recap_sent_once_when_customer_gives_email(client, db_session, outbox):
    pn = f"PN40{uuid.uuid4().hex[:6]}"
    tenant, product, _ = await _store(db_session, "TU", pn=pn)
    customer, order = await _customer_with_order(db_session, tenant, product, "221700000041")

    await _write(client, pn, "221700000041", "awa.client@example.com")
    recaps = [m for m in outbox if "commande" in m["subject"]]
    assert len(recaps) == 1
    mail = recaps[0]
    assert mail["to"] == "awa.client@example.com" and mail["from_name"] == "Boutique Awa" and mail["reply_to"] == tenant.email
    assert mail["subject"] == f"Ta commande {str(order.id)[:8].upper()} chez Boutique Awa"
    assert "- Robe wax x1 — 25 000 XOF" in mail["body"]

    await _write(client, pn, "221700000041", "Merci !")
    assert len([m for m in outbox if "commande" in m["subject"]]) == 1  # jamais deux fois


@pytest.mark.asyncio
async def test_recap_when_bob_records_the_email(client, db_session, outbox):
    pn = f"PN40{uuid.uuid4().hex[:6]}"
    tenant, product, _ = await _store(db_session, pn=pn)
    await _customer_with_order(db_session, tenant, product, "221700000042")
    llm = FakeLLMClient([tool_use_response("record_customer_email", {"email": "moussa@example.com"}), text_response("Merci !")])
    await _write(client, pn, "221700000042", "moussa arobase example point com", llm)
    [mail] = [m for m in outbox if "commande" in m["subject"]]
    assert mail["to"] == "moussa@example.com" and mail["subject"].startswith("Votre commande")


@pytest.mark.asyncio
@pytest.mark.parametrize("status, age_days", [(OrderStatus.CANCELLED, 0), (OrderStatus.PENDING, 1.1), (OrderStatus.PENDING, 4)])
async def test_no_recap_for_cancelled_or_old_orders(client, db_session, outbox, status, age_days):
    pn = f"PN40{uuid.uuid4().hex[:6]}"
    tenant, product, _ = await _store(db_session, pn=pn)
    await _customer_with_order(db_session, tenant, product, "221700000043", status=status, age_days=age_days)
    await _write(client, pn, "221700000043", "awa@example.com")
    assert [m for m in outbox if "commande" in m["subject"]] == []


@pytest.mark.asyncio
async def test_no_recap_without_order_or_in_dealership(client, db_session, outbox):
    pn = f"PN40{uuid.uuid4().hex[:6]}"
    await _store(db_session, pn=pn)
    await _write(client, pn, "221700000044", "awa@example.com")  # aucune commande
    pn2 = f"PN40{uuid.uuid4().hex[:6]}"
    tenant, product, _ = await _store(db_session, business_type=CAR_DEALERSHIP, pn=pn2)
    await _customer_with_order(db_session, tenant, product, "221700000045")
    await _write(client, pn2, "221700000045", "awa@example.com")
    assert [m for m in outbox if "commande" in m["subject"]] == []


@pytest.mark.asyncio
async def test_recap_uses_only_this_customers_order(db_session):
    tenant, product, _ = await _store(db_session)
    other, _ = await _customer_with_order(db_session, tenant, product, "221700000046")
    me = Customer(tenant_id=tenant.id, whatsapp_number="221700000047", email="me@example.com")
    db_session.add(me)
    await db_session.commit()
    assert await order_emails.recap_to_send(db_session, tenant, me) is None


@pytest.mark.asyncio
async def test_receipt_emailed_when_payment_confirmed(client, db_session, outbox, monkeypatch):
    class _Wa:
        def __init__(self, **kw):
            pass

        async def send_text_message(self, to, body):
            return None

    monkeypatch.setattr("app.api.orders.routes.WhatsAppClient", _Wa)
    tenant, product, email = await _store(db_session, "TU")
    with_email, order = await _customer_with_order(db_session, tenant, product, "221700000048", email="client@example.com")
    without_email, order2 = await _customer_with_order(db_session, tenant, product, "221700000049")
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}

    assert (await client.put(f"/api/v1/orders/{order.id}/mark-paid", headers=h)).status_code == 200
    assert (await client.put(f"/api/v1/orders/{order2.id}/mark-paid", headers=h)).status_code == 200

    [mail] = outbox
    assert mail["to"] == "client@example.com" and mail["subject"].startswith("Reçu de ta commande")
    reloaded = (await db_session.execute(select(Order).where(Order.id == order.id).execution_options(populate_existing=True))).scalar_one()
    assert reloaded.receipt_emailed_at is not None
    assert await order_emails.receipt_to_send(db_session, tenant, with_email, reloaded) is None  # une seule fois
    from sqlalchemy.orm.attributes import set_committed_value

    set_committed_value(reloaded, "receipt_emailed_at", None)  # lecture périmée : la base refuse quand même
    assert await order_emails.receipt_to_send(db_session, tenant, with_email, reloaded) is None


# --- Branchements restants : pied de page, campagne tu, plan gratuit (rappel et relance) --------------

def test_footer_goes_under_the_card():
    html = render_html("S", "Bonjour,\n\nTexte.\n\n—\nSe désinscrire : https://x.example/u", "Boutique Awa", for_customer=True)
    card, footer = html.split('class="card"', 1)[1].split('class="muted" style="padding: 16px 12px', 1)
    assert "x.example/u" not in card and "x.example/u" in footer
    assert "Cet email vous est envoyé" not in footer  # le pied de page de l'email remplace celui par défaut


@pytest.mark.asyncio
async def test_campaign_footer_uses_tu_for_a_tu_shop(db_session, monkeypatch):
    from app.services import campaign_service

    sent = []
    monkeypatch.setattr(campaign_service, "send_email", lambda **kw: sent.append(kw) or True)
    tenant, _, _ = await _store(db_session, "TU")
    db_session.add(Customer(tenant_id=tenant.id, whatsapp_number="221700000050", email="c@example.com", marketing_consent=True))
    await db_session.commit()
    user = (await db_session.execute(select(User).where(User.tenant_id == tenant.id))).scalar_one()
    await campaign_service.send_campaign(db_session, tenant.id, user.id, "Promo", "Nouveautés !", "Bonjour")
    [mail] = sent
    assert "Tu reçois cet email car tu as accepté" in mail["body"] and POWERED_BY not in mail["body"]


@pytest.mark.asyncio
async def test_free_plan_followup_email_says_powered_by(db_session):
    from app.services import appointment_outcome

    tenant = Tenant(name="Garage Awa", country="SN", currency="XOF", email=f"g{uuid.uuid4().hex[:5]}@example.com",
                    plan=TenantPlan.FREE, business_type=CAR_DEALERSHIP)
    db_session.add(tenant)
    await db_session.flush()
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000051", email="p@example.com")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id)
    db_session.add(conversation)
    await db_session.flush()
    appt = AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id, kind="ESSAI",
                              availability="x", status="CONFIRMED", outcome="NO_SHOW",
                              scheduled_at=datetime.now(timezone.utc) - timedelta(days=1))
    db_session.add(appt)
    await db_session.commit()
    sent = []
    channel = await appointment_outcome.send_followup(db_session, tenant, appt, datetime.now(timezone.utc),
                                                      send_email=lambda **kw: sent.append(kw) or True, send_whatsapp=None)
    assert channel == "EMAIL" and POWERED_BY in sent[0]["body"]


@pytest.mark.asyncio
async def test_free_plan_reminder_email_says_powered_by():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.core.database import Base
    from app.tests.test_appointments import SAT_10H_PARIS
    from app.workers import appointment_reminders

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        tenant = Tenant(name="Auto Plus", country="FR", currency="EUR", email="concession@example.com",
                        plan=TenantPlan.FREE, business_type=CAR_DEALERSHIP)
        db.add(tenant)
        await db.flush()
        customer = Customer(tenant_id=tenant.id, whatsapp_number="33600000009", email="client@example.com")
        db.add(customer)
        await db.flush()
        conv = Conversation(tenant_id=tenant.id, customer_id=customer.id)
        db.add(conv)
        await db.flush()
        db.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conv.id, customer_id=customer.id, kind="ESSAI",
                                  availability="samedi", status="CONFIRMED", scheduled_at=SAT_10H_PARIS))
        await db.commit()
    outbox = []
    await appointment_reminders.send_due_reminders(session_factory=factory, now=datetime(2026, 10, 2, 16, 30, tzinfo=timezone.utc),
                                                   send=lambda to, subject, body, **kw: outbox.append(body) or True)
    await engine.dispose()
    assert any(POWERED_BY in body for body in outbox)


@pytest.mark.asyncio
async def test_old_pending_order_is_never_recapped_only_the_new_one(client, db_session, outbox):
    """Cas réel du 02/10 : une commande de test du 28/09 restée en attente avait reçu un récapitulatif."""
    pn = f"PN40{uuid.uuid4().hex[:6]}"
    tenant, product, _ = await _store(db_session, pn=pn)
    customer, old = await _customer_with_order(db_session, tenant, product, "221700000052", age_days=4)
    await _write(client, pn, "221700000052", "awa@example.com")
    assert [m for m in outbox if "commande" in m["subject"]] == []  # la vieille commande : rien
    new = Order(tenant_id=tenant.id, customer_id=customer.id, status=OrderStatus.PENDING, total_amount=25000, currency="XOF")
    db_session.add(new)
    await db_session.flush()
    db_session.add(OrderItem(order_id=new.id, product_id=product.id, quantity=1, unit_price=25000, subtotal=25000))
    await db_session.commit()
    await _write(client, pn, "221700000052", "Merci")
    [mail] = [m for m in outbox if "commande" in m["subject"]]
    assert str(new.id)[:8].upper() in mail["subject"] and str(old.id)[:8].upper() not in mail["subject"]


@pytest.mark.asyncio
async def test_recap_is_reserved_once_even_if_two_treatments_race(db_session):
    """Deux traitements lisent la commande avant que l'un n'enregistre l'envoi : un seul récapitulatif."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    tenant, product, _ = await _store(db_session)
    customer, order = await _customer_with_order(db_session, tenant, product, "221700000053", email="c@example.com")
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False, class_=AsyncSession)
    async with factory() as a, factory() as b:
        ta, ca = await a.get(Tenant, tenant.id), await a.get(Customer, customer.id)
        tb, cb = await b.get(Tenant, tenant.id), await b.get(Customer, customer.id)
        await a.get(Order, order.id)
        await b.get(Order, order.id)  # les deux voient « pas encore envoyé »
        first = await order_emails.recap_to_send(a, ta, ca)
        await a.commit()
        # b a lu la commande AVANT l'envoi de a (lecture périmée, comme un second processus serveur)
        from sqlalchemy.orm.attributes import set_committed_value

        stale = await b.get(Order, order.id)
        set_committed_value(stale, "recap_emailed_at", None)
        second = await order_emails.recap_to_send(b, tb, cb)
        await b.commit()
    assert first is not None and second is None
