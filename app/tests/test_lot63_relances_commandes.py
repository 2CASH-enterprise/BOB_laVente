"""
Lot 63 — relances des commandes non payées (boutique en ligne), lien de prospection vers la démo du secteur,
aperçu du lien et visuels de prospection.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.followup_settings import TenantFollowupSettings
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.prospect import Prospect
from app.services import followup_service, monthly_report, order_reminders as rem, prospect_mailer, prospection as pros
from app.services.business_type import CAR_DEALERSHIP, INSURANCE_BROKER, ONLINE_STORE
from app.tests.test_lot53_courtier import _cabinet, _headers

NOW = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)  # lundi 12 octobre 2026, 10 h à Abidjan (UTC+0)


async def _shop(db, **fields):
    tenant = await _cabinet(db, business_type=ONLINE_STORE)
    tenant.name = "Boutique Awa"
    tenant.payment_link = "https://pay.wave.com/m/awa"
    for key, value in fields.items():
        setattr(tenant, key, value)
    await db.commit()
    return tenant


async def _customer(db, tenant, first="Fatou", email=None):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2250{uuid.uuid4().int % 10**9:09d}", first_name=first, email=email)
    db.add(customer)
    await db.commit()
    return customer


async def _conversation(db, tenant, customer, wrote_hours_ago=None):
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.flush()
    if wrote_hours_ago is not None:
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                       content="Je confirme la commande", created_at=NOW - timedelta(hours=wrote_hours_ago)))
    await db.commit()
    return conversation


async def _order(db, tenant, customer, hours_ago, conversation=None, status=OrderStatus.PENDING, total="15000"):
    product = Product(tenant_id=tenant.id, sku=f"S{uuid.uuid4().hex[:6]}", name="Robe wax", price=Decimal(total),
                      currency="XOF", stock_quantity=5)
    db.add(product)
    await db.flush()
    order = Order(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id if conversation else None,
                  status=status, total_amount=Decimal(total), currency="XOF", created_at=NOW - timedelta(hours=hours_ago))
    db.add(order)
    await db.flush()
    db.add(OrderItem(order_id=order.id, product_id=product.id, quantity=1, unit_price=Decimal(total), subtotal=Decimal(total)))
    await db.commit()
    return order


def _recorder(ok=True):
    sent = {"email": [], "whatsapp": []}

    def send_email(**mail):
        sent["email"].append(mail)
        return ok

    async def send_whatsapp(db, tenant, conversation, customer, text):
        sent["whatsapp"].append(text)
        return True

    return sent, send_email, send_whatsapp


# --- Règles ---------------------------------------------------------------------------------------------------

def test_due_steps():
    order = Order(status=OrderStatus.PENDING, created_at=NOW - timedelta(hours=2, minutes=59), reminders_sent=0)
    assert rem.due_step(order, NOW) is None
    order.created_at = NOW - timedelta(hours=3)
    assert rem.due_step(order, NOW) == 1
    order.created_at = NOW - timedelta(hours=48)
    assert rem.due_step(order, NOW) is None  # trop vieille pour une première relance
    order.created_at, order.reminders_sent = NOW - timedelta(hours=23, minutes=59), 1
    assert rem.due_step(order, NOW) is None
    order.created_at = NOW - timedelta(hours=24)
    assert rem.due_step(order, NOW) == 2
    order.last_reminder_at = NOW - timedelta(hours=11, minutes=59)
    assert rem.due_step(order, NOW) is None  # au moins 12 h entre les deux relances
    order.last_reminder_at = NOW - timedelta(hours=12)
    assert rem.due_step(order, NOW) == 2
    order.created_at = NOW - timedelta(hours=72)
    assert rem.due_step(order, NOW) is None
    order.created_at, order.reminders_sent = NOW - timedelta(hours=30), 2
    assert rem.due_step(order, NOW) is None  # jamais de troisième relance
    order.reminders_sent, order.status = 0, OrderStatus.PAID
    order.created_at = NOW - timedelta(hours=5)
    assert rem.due_step(order, NOW) is None


# --- Relances -------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_whatsapp_then_email_next_day(db_session):
    tenant = await _shop(db_session)
    fatou = await _customer(db_session, tenant, email="fatou@exemple.ci")
    conversation = await _conversation(db_session, tenant, fatou, wrote_hours_ago=4)
    order = await _order(db_session, tenant, fatou, hours_ago=3.9, conversation=conversation)
    sent, send_email, send_whatsapp = _recorder()

    report = await rem.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp)
    assert report == {"whatsapp": 1, "email": 0, "none": 0, "failed": 0}
    ref = str(order.id)[:8].upper()
    assert sent["whatsapp"][0] == (
        f"Bonjour Fatou, ici Boutique Awa. Votre commande {ref} (15 000 XOF) est bien enregistrée et attend votre paiement.\n\n"
        "- Robe wax x1 — 15 000 XOF\n\nPayer : https://pay.wave.com/m/awa\n"
        "Envoyez ici la capture d'écran du paiement pour recevoir votre reçu.\nUne question ? Répondez simplement à ce message.")
    await db_session.refresh(order)
    assert order.reminders_sent == 1 and order.last_reminder_channel == "WHATSAPP" and order.first_reminder_at is not None
    first_at = order.first_reminder_at
    assert (await rem.run_for_tenant(db_session, tenant, NOW + timedelta(hours=1), send_email, send_whatsapp))["whatsapp"] == 0

    # Le lendemain matin (9 h 30) : la fenêtre de 20 h est fermée → email (message de service)
    early = NOW + timedelta(hours=22, minutes=30)  # 8 h 30 : pas avant 9 h
    assert (await rem.run_for_tenant(db_session, tenant, early, send_email, send_whatsapp))["email"] == 0
    tomorrow = NOW + timedelta(hours=23, minutes=30)
    report = await rem.run_for_tenant(db_session, tenant, tomorrow, send_email, send_whatsapp)
    assert report["email"] == 1 and len(sent["whatsapp"]) == 1
    mail = sent["email"][0]
    assert mail["to"] == "fatou@exemple.ci" and mail["from_name"] == "Boutique Awa" and mail["reply_to"] == tenant.email
    assert mail["subject"] == f"Rappel : votre commande {ref} chez Boutique Awa attend votre paiement"
    assert "toujours en attente de paiement" in mail["body"] and "Payer ma commande : https://pay.wave.com/m/awa" in mail["body"]
    assert "Répondez simplement à cet email" in mail["body"] and "/unsubscribe" in mail["body"].lower()
    assert "List-Unsubscribe" in mail["extra_headers"]
    await db_session.refresh(order)
    assert order.reminders_sent == 2 and order.last_reminder_channel == "EMAIL" and order.first_reminder_at == first_at
    note = (await db_session.execute(select(Message).where(Message.message_type == "order_reminder_email"))).scalar_one()
    assert note.sender == MessageSender.SYSTEM and note.conversation_id == conversation.id
    # Plus rien ensuite
    await rem.run_for_tenant(db_session, tenant, tomorrow + timedelta(hours=5), send_email, send_whatsapp)
    assert len(sent["email"]) == 1


@pytest.mark.asyncio
async def test_email_without_offers_consent_but_never_after_unsubscribe(db_session):
    tenant = await _shop(db_session, address_form="TU", payment_link=None)
    awa = await _customer(db_session, tenant, first=None, email="awa@exemple.ci")
    awa.marketing_consent = False  # pas d'accord pour les offres : la relance de SA commande part quand même
    gone = await _customer(db_session, tenant, email="gone@exemple.ci")
    gone.marketing_consent_withdrawn_at = NOW - timedelta(days=2)
    await db_session.commit()
    first = await _order(db_session, tenant, awa, hours_ago=5)
    second = await _order(db_session, tenant, gone, hours_ago=5)
    sent, send_email, send_whatsapp = _recorder()
    report = await rem.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp)
    assert report == {"whatsapp": 0, "email": 1, "none": 1, "failed": 0}
    mail = sent["email"][0]
    assert mail["to"] == "awa@exemple.ci" and mail["subject"].startswith("Ta commande ") and mail["subject"].endswith("attend ton paiement")
    assert mail["body"].startswith("Bonjour,\n\nTa commande est bien enregistrée") and "Payer ma commande" not in mail["body"]
    assert "tu recevras ton reçu" in mail["body"] and "Tu reçois cet email au sujet de ta commande" in mail["body"]
    await db_session.refresh(second)
    # Rien n'a pu partir : l'étape est comptée, la commande reste « à appeler » (pas de première relance réelle)
    assert second.reminders_sent == 1 and second.last_reminder_channel == "NONE" and second.first_reminder_at is None
    await db_session.refresh(first)
    assert first.first_reminder_at is not None


@pytest.mark.asyncio
async def test_failed_email_is_retried_next_hour(db_session):
    tenant = await _shop(db_session)
    awa = await _customer(db_session, tenant, email="awa@exemple.ci")
    order = await _order(db_session, tenant, awa, hours_ago=4)
    _, failing, send_whatsapp = _recorder(ok=False)
    report = await rem.run_for_tenant(db_session, tenant, NOW, failing, send_whatsapp)
    assert report["failed"] == 1 and report["email"] == 0
    await db_session.refresh(order)
    assert order.reminders_sent == 0
    sent, send_email, _ = _recorder()
    assert (await rem.run_for_tenant(db_session, tenant, NOW + timedelta(hours=1), send_email, send_whatsapp))["email"] == 1


@pytest.mark.asyncio
async def test_stops_when_customer_writes_or_a_human_is_on_it(db_session):
    tenant = await _shop(db_session)
    wrote = await _customer(db_session, tenant, email="a@exemple.ci")
    conv = await _conversation(db_session, tenant, wrote)
    order = await _order(db_session, tenant, wrote, hours_ago=5, conversation=conv)
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conv.id, sender=MessageSender.CUSTOMER,
                           content="Je paie ce soir", created_at=NOW - timedelta(hours=1)))
    thanks = await _customer(db_session, tenant, email="b@exemple.ci")
    conv_thanks = await _conversation(db_session, tenant, thanks)
    order_thanks = await _order(db_session, tenant, thanks, hours_ago=5, conversation=conv_thanks)
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conv_thanks.id, sender=MessageSender.CUSTOMER,
                           content="Merci", created_at=NOW - timedelta(hours=4, minutes=55)))  # dans les 10 min
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conv_thanks.id, sender=MessageSender.AI,
                           content="Avec plaisir", created_at=NOW - timedelta(hours=2)))  # Bob ne compte pas
    human = await _customer(db_session, tenant, email="c@exemple.ci")
    conv_human = await _conversation(db_session, tenant, human)
    conv_human.status = ConversationStatus.WAITING_HUMAN
    await _order(db_session, tenant, human, hours_ago=5, conversation=conv_human)
    phone = await _customer(db_session, tenant, email="d@exemple.ci")
    conv_phone = await _conversation(db_session, tenant, phone)
    conv_phone.phone_reply_at = NOW - timedelta(hours=3)
    await _order(db_session, tenant, phone, hours_ago=5, conversation=conv_phone)
    await db_session.commit()
    sent, send_email, send_whatsapp = _recorder()
    report = await rem.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp)
    # Seul le « merci » juste après la commande ne compte pas (fenêtre ouverte : relance sur WhatsApp)
    assert report["whatsapp"] == 1 and report["email"] == 0 and len(sent["whatsapp"]) == 1
    await db_session.refresh(order)
    await db_session.refresh(order_thanks)
    assert order.reminders_sent == 0 and order_thanks.reminders_sent == 1
    assert await rem.stop_reason(db_session, tenant, order, conv, NOW) == "customer_wrote"


@pytest.mark.asyncio
async def test_nothing_for_paid_cancelled_demo_pause_setting_hours_or_other_sector(db_session):
    sent, send_email, send_whatsapp = _recorder()
    tenant = await _shop(db_session)
    awa = await _customer(db_session, tenant, email="awa@exemple.ci")
    await _order(db_session, tenant, awa, hours_ago=5, status=OrderStatus.PAID)
    await _order(db_session, tenant, awa, hours_ago=5, status=OrderStatus.CANCELLED)
    assert (await rem.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp))["email"] == 0
    await _order(db_session, tenant, awa, hours_ago=5)
    for hour in (8, 20):  # entre 9 h et 20 h, heure du pays
        assert (await rem.run_for_tenant(db_session, tenant, NOW.replace(hour=hour), send_email, send_whatsapp))["email"] == 0
    tenant.order_reminders_enabled = False
    await db_session.commit()
    assert (await rem.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp))["email"] == 0
    tenant.order_reminders_enabled, tenant.is_demo = True, True
    await db_session.commit()
    assert (await rem.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp))["email"] == 0
    tenant.is_demo, tenant.paid_until = False, date(2026, 9, 1)  # abonnement non renouvelé : Bob en pause
    await db_session.commit()
    assert (await rem.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp))["email"] == 0
    tenant.paid_until, tenant.business_type = None, CAR_DEALERSHIP
    await db_session.commit()
    assert (await rem.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp))["email"] == 0
    assert sent["email"] == []
    tenant.business_type = ONLINE_STORE
    await db_session.commit()
    assert (await rem.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp))["email"] == 1


@pytest.mark.asyncio
async def test_worker_runs_each_shop(db_session):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.workers.order_reminders import check_order_reminders

    tenant = await _shop(db_session)
    await _order(db_session, tenant, await _customer(db_session, tenant, email="w@exemple.ci"), hours_ago=4)
    broker = await _cabinet(db_session, business_type=INSURANCE_BROKER)
    await _order(db_session, broker, await _customer(db_session, broker, email="x@exemple.ci"), hours_ago=4)
    sent, send_email, send_whatsapp = _recorder()
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    total = await check_order_reminders(factory, NOW, send_email, send_whatsapp)
    assert total["email"] >= 1 and "x@exemple.ci" not in [m["to"] for m in sent["email"]]


@pytest.mark.asyncio
async def test_offers_followup_steps_aside_during_order_reminders(db_session):
    tenant = await _shop(db_session)
    awa = await _customer(db_session, tenant, email="awa@exemple.ci")
    awa.marketing_consent = True
    conv = await _conversation(db_session, tenant, awa, wrote_hours_ago=6)
    settings = TenantFollowupSettings(tenant_id=tenant.id, enabled=True)
    db_session.add(settings)
    await db_session.commit()
    await _order(db_session, tenant, awa, hours_ago=6, conversation=conv)
    sent, send_email, _ = _recorder()
    assert await rem.in_progress(db_session, tenant, awa.id, NOW)
    assert not await rem.in_progress(db_session, tenant, awa.id, NOW + timedelta(hours=67))  # passé 72 h
    assert await followup_service.send_followup(db_session, tenant.id, conv, settings, NOW, send_email) is False
    assert sent["email"] == [] and conv.followup_stage == 1
    tenant.order_reminders_enabled = False  # relances de commandes coupées : la relance « offres » repart
    await db_session.commit()
    assert await followup_service.send_followup(db_session, tenant.id, conv, settings, NOW, send_email) is True


# --- Rapport mensuel, réglage, tableau de bord -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_monthly_report_line(db_session):
    tenant = await _shop(db_session)
    awa = await _customer(db_session, tenant)
    paid = await _order(db_session, tenant, awa, hours_ago=100, status=OrderStatus.PAID, total="20000")
    paid.first_reminder_at, paid.paid_at = NOW - timedelta(hours=96), NOW - timedelta(hours=90)
    before = await _order(db_session, tenant, awa, hours_ago=100, status=OrderStatus.PAID, total="7000")
    before.first_reminder_at, before.paid_at = NOW - timedelta(hours=96), NOW - timedelta(hours=97)  # payée avant
    still = await _order(db_session, tenant, awa, hours_ago=50)
    still.first_reminder_at = NOW - timedelta(hours=47)
    await db_session.commit()
    start, end = NOW - timedelta(days=30), NOW
    data = await monthly_report.build(db_session, tenant, start, end)
    line = dict(data["bob"]["savings"])["Commandes relancées par Bob → payées ensuite"]
    assert line.startswith("3 → 1 (20") and "XOF" in line
    assert rem.summary([paid, before, still], start, end)[1:] == (1, 20000.0)
    assert rem.summary([paid, before, still], NOW - timedelta(hours=95), end) == (1, 0, 0.0)  # relancées avant la période
    other = await _shop(db_session)
    assert "Commandes relancées par Bob → payées ensuite" not in dict(
        (await monthly_report.build(db_session, other, start, end))["bob"]["savings"])


@pytest.mark.asyncio
async def test_setting_api_and_order_fields(client, db_session):
    from app.models.user import Role, User
    from app.core.security import hash_password

    tenant = await _shop(db_session)
    headers = await _headers(client, tenant)
    r = await client.get("/api/v1/tenants/me/order-reminders", headers=headers)
    assert r.status_code == 200 and r.json() == {"enabled": True, "payment_link": True}
    r = await client.put("/api/v1/tenants/me/order-reminders", headers=headers, json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    await db_session.refresh(tenant)
    assert tenant.order_reminders_enabled is False
    agent = f"agent{uuid.uuid4().hex[:6]}@l63.ci"
    db_session.add(User(tenant_id=tenant.id, email=agent, hashed_password=hash_password("x"), full_name="A", role=Role.AGENT))
    await db_session.commit()
    token = (await client.post("/api/v1/auth/login", data={"username": agent, "password": "x"})).json()["access_token"]
    assert (await client.put("/api/v1/tenants/me/order-reminders", headers={"Authorization": f"Bearer {token}"},
                             json={"enabled": True})).status_code == 403
    broker = await _cabinet(db_session, business_type=INSURANCE_BROKER)
    assert (await client.get("/api/v1/tenants/me/order-reminders", headers=await _headers(client, broker))).status_code == 403

    order = await _order(db_session, tenant, await _customer(db_session, tenant), hours_ago=5)
    order.reminders_sent, order.first_reminder_at, order.last_reminder_at = 1, NOW, NOW
    order.last_reminder_channel = "WHATSAPP"
    await db_session.commit()
    rows = (await client.get("/api/v1/orders", headers=headers)).json()
    row = next(o for o in rows if o["id"] == str(order.id))
    assert row["reminders_sent"] == 1 and row["last_reminder_channel"] == "WHATSAPP" and row["first_reminder_at"]


def test_dashboard_has_the_setting():
    html = Path("app/static/dashboard/index.html").read_text(encoding="utf-8")
    assert 'id="order-reminders"' in html and "function orderReminderNotice(" in html and "loadOrderReminders();" in html
    assert "Payée après la relance de Bob" in html and "appelez-le" in html


# --- Prospection : démo du secteur, aperçu du lien, visuels ------------------------------------------------------

BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15"


async def _prospect(db, sector="CAR_DEALERSHIP"):
    prospect = Prospect(company="Auto Plus", contact_name="Serge", sector=sector, code=uuid.uuid4().hex[:6], status="ACTIVE")
    db.add(prospect)
    await db.commit()
    return prospect


@pytest.mark.asyncio
async def test_link_is_the_demo_of_the_sector(client, db_session):
    from app.core.config import get_settings

    base = get_settings().public_base_url.rstrip("/")
    prospect = await _prospect(db_session)
    assert pros.link(prospect.code, "CAR_DEALERSHIP") == f"{base}/demo/?secteur=concession&p={prospect.code}"
    assert pros.link("abc", None) == f"{base}/demo/?p=abc"
    assert pros.demo_url(None) == f"{base}/demo/" and pros.demo_url("<x>") == f"{base}/demo/"
    assert pros.demo_url("INSURANCE_BROKER") == f"{base}/demo/?secteur=assurance"
    assert pros.sector_from_slug("Concession") == "CAR_DEALERSHIP" and pros.sector_from_slug("ONLINE_STORE") == "ONLINE_STORE"
    assert pros.sector_from_slug("<x>") is None and pros.sector_from_slug(None) is None
    r = await client.get(f"/p/{prospect.code}", headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"{base}/demo/?secteur=concession&p={prospect.code}"
    demo = Path("app/static/instant-demo/index.html").read_text(encoding="utf-8")
    assert 'get("secteur")' in demo and "concession: \"CAR_DEALERSHIP\"" in demo and "if (code && SECTORS[code]) chooseSector(code);" in demo


@pytest.mark.asyncio
async def test_link_preview_for_bots(client, db_session):
    from app.core.config import get_settings

    base = get_settings().public_base_url.rstrip("/")
    prospect = await _prospect(db_session, sector="INSURANCE_BROKER")
    r = await client.get(f"/demo/?secteur=assurance&p={prospect.code}", headers={"User-Agent": "WhatsApp/2.24.1 A"})
    assert r.status_code == 200 and "set-cookie" not in r.headers
    assert f'<meta property="og:image" content="{base}/visuels/prospection-courtier.jpg">' in r.text
    assert "l&#x27;assistant WhatsApp de votre cabinet" in r.text and "sans jamais donner de prix" in r.text
    assert f'<meta property="og:url" content="{base}/demo/?secteur=assurance&amp;p={prospect.code}">' in r.text
    assert r.text.index("og:image") < r.text.index("</head>")
    await db_session.refresh(prospect)
    assert (prospect.click_count or 0) == 0  # un aperçu n'est pas un clic
    # Sans code, le secteur seul suffit à l'aperçu ; le secteur du prospect sert si l'adresse n'en a pas
    generic = await client.get("/demo/?secteur=commerce", headers={"User-Agent": "WhatsApp/2.24.1 A"})
    assert "prospection-commerce.jpg" in generic.text and "set-cookie" not in generic.headers
    own = await client.get(f"/demo/?p={prospect.code}", headers={"User-Agent": "WhatsApp/2.24.1 A"})
    assert "prospection-courtier.jpg" in own.text


@pytest.mark.asyncio
async def test_visuals_are_served_and_small(client):
    for name in pros.VISUALS.values():
        path = Path("app/static/visuels") / name
        assert path.exists() and path.stat().st_size < 300_000  # WhatsApp n'affiche pas les aperçus trop lourds
        r = await client.get(f"/visuels/{name}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_email_banner_and_demo_button():
    prospect = Prospect(company="Boutique Awa", contact_name="Awa Koné", sector="ONLINE_STORE", code="abc234")
    link = pros.link("abc234", "ONLINE_STORE")
    subject, body = prospect_mailer.DEFAULTS["ONLINE_STORE"][0]
    _, text, html = prospect_mailer.render(prospect, subject, body)
    # Le lien seul sur sa ligne devient la bannière puis le bouton « Voir la démo » (plus d'adresse affichée)
    intro = html.index("avec votre propre catalogue :")
    banner = html.index("/visuels/prospection-commerce.jpg")
    button = html.index(">Voir la démo</a>")
    assert intro < banner < button < html.index("Bonne journée")
    href = link.replace("&", "&amp;")
    assert f'<a href="{href}"><img' in html and f'<a href="{href}" style="display:inline-block;' in html
    assert html.count(f'href="{href}"') == 2 and f">{href}<" not in html
    assert f"\nVoir la démo : {link}\n" in text and "prospection-commerce" not in text
    # Lien au milieu d'une phrase : gardé, bannière et bouton après le paragraphe
    _, text, html = prospect_mailer.render(prospect, "Objet", f"Bonjour,\n\nVoici : {link} pour essayer.\n\nÀ bientôt,")
    assert html.index("pour essayer.") < html.index(">Voir la démo<") < html.index("À bientôt")
    assert f"Voici : {link} pour essayer." in text
    _, _, html = prospect_mailer.render(prospect, "Objet", f"Bonjour,\n\n{link}\n\nEncore : {link}\n\nÀ bientôt,")
    assert html.count("prospection-commerce.jpg") == 1 and html.count(">Voir la démo<") == 1  # une seule fois
    plain = Prospect(company="X", sector="CAR_DEALERSHIP", code="zz9999")
    _, _, html = prospect_mailer.render(plain, "Objet", "Bonjour,\n\nSans lien.\n\nÀ bientôt,")
    assert html.index("Sans lien.") < html.index("prospection-concession.jpg") < html.index(">Voir la démo<") < html.index("À bientôt")


def test_whatsapp_message_ends_with_the_demo_link():
    p = Prospect(company="Auto Plus", contact_name="Serge K", sector="CAR_DEALERSHIP", code="abc234")
    text = pros.message(p, "Koffi")
    assert text.endswith(f"pour Auto Plus.\n\n👉 Voir la démo : {pros.link('abc234', 'CAR_DEALERSHIP')}")
    assert text.endswith("/demo/?secteur=concession&p=abc234")


@pytest.mark.asyncio
async def test_superadmin_fiche_offers_the_visual():
    html = Path("app/static/superadmin/index.html").read_text(encoding="utf-8")
    assert "Télécharger le visuel" in html and 'download="bob-visuel-' in html
    from app.api.prospects import routes

    assert "visual_url" in Path(routes.__file__).read_text(encoding="utf-8")
