"""
Lot 108 — QR code des tables (sur place, table connue, rien à écrire), rappel de table 2 h avant, alerte au restaurant
45 min avant une commande pour plus tard, statut « Prête » qui prévient le client.
"""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.database import Base
from app.models.appointment_request import STATUS_CONFIRMED, STATUS_REQUESTED, AppointmentRequest
from app.models.conversation import Message, MessageSender
from app.models.delivery import Delivery, DeliveryStatus
from app.models.order import Order, OrderStatus
from app.models.table_qr_code import TableQrCode
from app.models.whatsapp_account import WhatsAppAccount
from app.services import appointment_service, delivery_notice, guided_flow, notifications, resto_flow, shop_cart
from app.tests.test_lot53_courtier import _cabinet, _conversation, _headers
from app.tests.test_lot94_parcours_boutons import _WA, _text, guided_on  # noqa: F401 — fixture
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture
from app.tests.test_lot106_restaurant import _dish, _resto
from app.services.business_type import RESTAURANT

HTML = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)  # Abidjan : UTC


@pytest.fixture(autouse=True)
def _guided(monkeypatch):
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)


async def _tap(db, tenant, customer, conversation, reply_id):
    turn = await guided_flow.handle(db, tenant, None, f"g:{reply_id}", None, False, customer, conversation)
    await db.commit()
    return turn


def _titles(action) -> list[str]:
    if action["type"] == "list":
        return [row[1] for row in action["rows"]]
    return [b[1] for b in action["buttons"]]


async def _phone(db, tenant):
    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalar_one()
    account.display_phone_number = "+225 07 00 00 00 01"
    await db.commit()


# --- QR codes des tables ---------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tables_are_created_in_one_go(client, db_session):
    tenant = await _resto(db_session)
    headers = await _headers(client, tenant)
    r = await client.post("/api/v1/table-qr-codes", headers=headers, json={"first": 1, "last": 12})
    assert r.status_code == 201 and [t["label"] for t in r.json()] == [str(n) for n in range(1, 13)]
    first_code = r.json()[0]["code"]
    again = (await client.post("/api/v1/table-qr-codes", headers=headers, json={"first": 10, "last": 14})).json()
    assert len(again) == 14 and again[0]["code"] == first_code  # une table existante garde son QR : rien à réimprimer
    assert (await client.post("/api/v1/table-qr-codes", headers=headers, json={"first": 5, "last": 2})).status_code == 422
    assert (await client.post("/api/v1/table-qr-codes", headers=headers, json={"first": 1, "last": 101})).status_code == 422
    listed = (await client.get("/api/v1/table-qr-codes", headers=headers)).json()
    assert [t["label"] for t in listed][:3] == ["1", "2", "3"] and listed[-1]["short_path"].startswith("/t/")
    assert (await client.delete(f"/api/v1/table-qr-codes/{listed[0]['id']}", headers=headers)).status_code == 204
    assert len((await client.get("/api/v1/table-qr-codes", headers=headers)).json()) == 13
    shop = await _cabinet(db_session, business_type="ONLINE_STORE")
    assert (await client.get("/api/v1/table-qr-codes", headers=await _headers(client, shop))).status_code == 403
    other = await _resto(db_session)
    assert (await client.delete(f"/api/v1/table-qr-codes/{listed[1]['id']}",
                                headers=await _headers(client, other))).status_code == 404  # jamais la table d'un autre


@pytest.mark.asyncio
async def test_the_table_link_opens_whatsapp_with_its_number(client, db_session):
    tenant = await _resto(db_session)
    await _phone(db_session, tenant)
    db_session.add(TableQrCode(tenant_id=tenant.id, code="TAB7AWA", label="7", scan_count=0))
    await db_session.commit()
    bot = await client.get("/t/TAB7AWA", headers={"user-agent": "WhatsApp/2.23"})
    person = await client.get("/t/TAB7AWA", headers={"user-agent": "Mozilla/5.0"})
    assert person.status_code == 200 and "wa.me/2250700000001?text=Bonjour%2C%20je%20suis%20%C3%A0%20la%20table%207%20%5BT%3ATAB7AWA%5D" in person.text
    assert bot.status_code == 200 and "Table 7 — Chez Tante Awa" in bot.text
    table = (await db_session.execute(select(TableQrCode).where(TableQrCode.code == "TAB7AWA"))).scalar_one()
    await db_session.refresh(table)
    assert table.scan_count == 1  # l'aperçu du lien n'est pas un scan
    assert (await client.get("/t/INCONNU")).status_code == 404


@pytest.mark.asyncio
async def test_scanning_the_table_opens_bob_at_that_table(client, db_session, guided_on):  # noqa: F811
    tenant = await _resto(db_session)
    db_session.add(TableQrCode(tenant_id=tenant.id, code="TAB12", label="12", scan_count=0))
    await db_session.commit()
    guided_on({"intents": [], "objections": []}, [])
    r = await client.post("/webhooks/whatsapp", json=_text(tenant.pnid, "2250102030405", "Bonjour, je suis à la table 12 [T:TAB12]"))
    assert r.json()["guided"] is True
    sent = _WA.sent[-1]
    assert "Vous êtes à la *table 12*" in sent["body"] and "[T:" not in sent["body"]
    assert [row[1] for row in sent["rows"]] == ["Commander", "Voir la carte", "Appeler un serveur"]
    from app.models.customer import Customer

    customer = (await db_session.execute(select(Customer).where(Customer.tenant_id == tenant.id))).scalar_one()
    assert customer.acquisition_source == "QR" and customer.acquisition_detail == "Table 12"
    # Le code d'une table d'un AUTRE restaurant n'ouvre rien ici.
    other = await _resto(db_session)
    db_session.add(TableQrCode(tenant_id=other.id, code="TABX", label="3", scan_count=0))
    await db_session.commit()
    from app.api.webhooks.whatsapp import _entry_of

    assert await _entry_of(db_session, tenant.id, "T", "TABX", "Bonjour, je suis à la table 3") is None
    assert await _entry_of(db_session, other.id, "T", "TABX", "Bonjour, je suis à la table 3") == {"table": "3"}


@pytest.mark.asyncio
async def test_at_the_table_order_skips_mode_and_table_number(db_session):
    tenant = await _resto(db_session)
    dish = await _dish(db_session, tenant)
    customer, conversation = await _conversation(db_session, tenant)
    welcome = (await resto_flow.at_table(db_session, tenant, False, customer, "12")).actions[0]
    await db_session.commit()
    assert "table 12" in welcome["body"] and "Réserver une table" not in _titles(welcome)
    await _tap(db_session, tenant, customer, conversation, f"fq:{dish.id}:-:2")
    recap = (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]  # ni mode, ni numéro : le récapitulatif
    assert "Sur place, table 12" in recap["body"] and "Paiement : au restaurant" in recap["body"]
    await _tap(db_session, tenant, customer, conversation, "fw")
    order = (await db_session.execute(select(Order).where(Order.tenant_id == tenant.id))).scalar_one()
    assert order.fulfillment == "DINE_IN" and order.table_number == "12"
    # Toujours à table : une deuxième commande (boissons) va aussi à la table 12.
    await _tap(db_session, tenant, customer, conversation, f"fq:{dish.id}:-:1")
    assert "Sur place, table 12" in (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]["body"]
    waiter = await _tap(db_session, tenant, customer, conversation, "human")
    assert waiter.handoff and waiter.handoff_reason == "Table 12 : le client appelle un serveur"
    assert "table 12" in waiter.handoff_text


@pytest.mark.asyncio
async def test_the_table_is_forgotten_after_three_hours(db_session, monkeypatch):
    tenant = await _resto(db_session)
    dish = await _dish(db_session, tenant)
    customer, conversation = await _conversation(db_session, tenant)
    await resto_flow.at_table(db_session, tenant, False, customer, "4")
    await _tap(db_session, tenant, customer, conversation, f"fq:{dish.id}:-:1")
    later = datetime.now(timezone.utc) + timedelta(hours=3, minutes=1)
    monkeypatch.setattr(resto_flow, "_now", lambda: later)
    modes = (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]
    assert _titles(modes) == ["À emporter", "Livraison", "Sur place"]  # rentré chez lui : la question revient
    welcome = (await _tap(db_session, tenant, customer, conversation, "menu")).actions[0]
    assert "Réserver une table" in _titles(welcome)


@pytest.mark.asyncio
async def test_without_dine_in_the_table_qr_opens_the_usual_welcome(db_session):
    tenant = await _resto(db_session, dine_in_enabled=False)
    customer, _ = await _conversation(db_session, tenant)
    welcome = (await resto_flow.at_table(db_session, tenant, False, customer, "2")).actions[0]
    assert "table" not in welcome["body"] and "Réserver une table" in _titles(welcome)


# --- « Prête » ---------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ready_tells_the_customer_once(db_session):
    tenant = await _resto(db_session)
    customer, conversation = await _conversation(db_session, tenant)
    customer.email = "awa@example.ci"
    order = Order(tenant_id=tenant.id, customer_id=customer.id, conversation_id=None, status=OrderStatus.PENDING,
                  total_amount=4500, currency="XOF", fulfillment="DINE_IN", table_number="7", created_by="IA")
    db_session.add(order)
    await db_session.flush()
    delivery = Delivery(tenant_id=tenant.id, order_id=order.id, status=DeliveryStatus.READY)
    db_session.add(delivery)
    await db_session.commit()
    mails = []
    first = await delivery_notice.notify(db_session, tenant, order, delivery, "u1", now=NOW, send_email=lambda **m: mails.append(m) or True)
    second = await delivery_notice.notify(db_session, tenant, order, delivery, "u1", now=NOW, send_email=lambda **m: mails.append(m) or True)
    assert first == {"step": "READY", "channel": "EMAIL"} and second == {"step": None, "channel": None}
    assert "est prête : on vous l'apporte à la table 7." in mails[0]["body"] and "est prête" in mails[0]["subject"]
    delivery.status = DeliveryStatus.DELIVERED  # « Servie » : ni « en route » ni « livrée » pour un plat sur place
    assert delivery_notice.step_due(delivery, order, tenant) is None
    order.fulfillment = "DELIVERY"
    assert delivery_notice.step_due(delivery, order, tenant) == "DELIVERED"
    pickup = Order(fulfillment="PICKUP", total_amount=1, currency="XOF", id=uuid.uuid4())
    assert "vous pouvez venir la chercher" in delivery_notice.ready_text(tenant, customer, pickup, False)
    assert "tu peux venir la chercher" in delivery_notice.ready_text(tenant, customer, pickup, True)


@pytest.mark.asyncio
async def test_tracking_a_restaurant_order(db_session):
    tenant = await _resto(db_session)
    customer, _ = await _conversation(db_session, tenant)
    order = Order(tenant_id=tenant.id, customer_id=customer.id, status=OrderStatus.PENDING, total_amount=4500,
                  currency="XOF", fulfillment="PICKUP", created_by="IA")
    db_session.add(order)
    await db_session.commit()
    body = (await shop_cart.track(db_session, tenant, False, customer)).actions[0]["body"]
    assert "le restaurant la prépare" in body and "preuve de paiement" not in body
    db_session.add(Delivery(tenant_id=tenant.id, order_id=order.id, status=DeliveryStatus.READY))
    await db_session.commit()
    turn = (await shop_cart.track(db_session, tenant, False, customer)).actions[0]
    assert "prête : vous pouvez venir la chercher" in turn["body"] and _titles(turn) == ["Voir la carte", "Parler au restaurant"]


@pytest.mark.asyncio
async def test_ready_status_is_accepted_by_the_dashboard_api(client, db_session):
    tenant = await _resto(db_session)
    customer, _ = await _conversation(db_session, tenant)
    order = Order(tenant_id=tenant.id, customer_id=customer.id, status=OrderStatus.PENDING, total_amount=4500,
                  currency="XOF", fulfillment="PICKUP", created_by="IA")
    db_session.add(order)
    await db_session.commit()
    r = await client.put(f"/api/v1/orders/{order.id}/delivery", headers=await _headers(client, tenant),
                         json={"status": "READY", "notify_customer": True})
    assert r.status_code == 200 and r.json()["status"] == "READY" and r.json()["notified_step"] == "READY"
    assert r.json()["notified_by"] is None  # ni WhatsApp ouvert, ni email : personne n'est prévenu, et c'est dit


# --- Rappel de table 2 h avant -----------------------------------------------------------------------------------------

def _table(when, created, **fields):
    return AppointmentRequest(kind="TABLE", status=STATUS_CONFIRMED, scheduled_at=when, created_at=created,
                              vehicle_label="4 personnes", availability="Samedi à 13 h 45", **fields)


def test_table_reminder_is_due_two_hours_before():
    zone = timezone.utc
    booked = _table(NOW + timedelta(hours=1, minutes=50), NOW - timedelta(days=1))
    due = appointment_service.reminder_due(booked, NOW, zone, "awa@example.ci")
    assert due.whatsapp and due.customer and not due.staff  # pas d'email au restaurant pour chaque table
    assert not appointment_service.reminder_due(_table(NOW + timedelta(hours=2, minutes=10), NOW - timedelta(days=1)), NOW, zone, None).whatsapp
    assert not appointment_service.reminder_due(_table(NOW - timedelta(minutes=5), NOW - timedelta(days=1)), NOW, zone, None).whatsapp
    # Réservée il y a 20 min pour dans 1 h 30 : la confirmation vient d'arriver, pas de rappel.
    assert not appointment_service.reminder_due(_table(NOW + timedelta(hours=1, minutes=30), NOW - timedelta(minutes=20)), NOW, zone, None).whatsapp
    text = appointment_service.whatsapp_reminder_message(booked, "Chez Tante Awa", zone)
    assert text.startswith("Bonjour ! Petit rappel : votre table pour 4 personnes chez Chez Tante Awa est réservée pour aujourd'hui à 13 h 50.")
    assert "demain" not in text
    subject, body = appointment_service.customer_reminder_email(booked, "Chez Tante Awa", zone)
    assert subject == "Rappel de votre table chez Chez Tante Awa" and "À tout à l'heure" in body


@pytest.mark.asyncio
async def test_worker_reminds_a_table_once_on_whatsapp_and_skips_the_email():
    from app.workers import appointment_reminders

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        tenant = await _resto(db)
        customer, conversation = await _conversation(db, tenant)
        customer.email = "awa@example.ci"
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER, content="Merci",
                       message_type="text", created_at=NOW - timedelta(hours=1)))
        db.add(_table(NOW + timedelta(hours=1, minutes=45), NOW - timedelta(days=1), tenant_id=tenant.id,
                      customer_id=customer.id, conversation_id=conversation.id))
        silent, silent_conv = await _conversation(db, tenant)  # n'a pas écrit depuis 20 h : email, jamais de modèle
        silent.email = "kone@example.ci"
        db.add(_table(NOW + timedelta(hours=1), NOW - timedelta(days=2), tenant_id=tenant.id,
                      customer_id=silent.id, conversation_id=silent_conv.id))
        await db.commit()
    emails, whatsapps, templates = [], [], []

    async def wa(db, tenant, conversation, customer, text, buttons=None):
        whatsapps.append((text, buttons))
        return True

    async def template(*a, **kw):
        templates.append(a)
        return True

    first = await appointment_reminders.send_due_reminders(session_factory=factory, now=NOW, send=lambda **m: emails.append(m) or True,
                                                           send_whatsapp=wa, send_template=template)
    second = await appointment_reminders.send_due_reminders(session_factory=factory, now=NOW + timedelta(minutes=15),
                                                            send=lambda **m: emails.append(m) or True, send_whatsapp=wa,
                                                            send_template=template)
    assert first["whatsapp"] == 1 and second["whatsapp"] == 0 and first["staff"] == 0
    assert len(whatsapps) == 1 and "votre table pour 4 personnes" in whatsapps[0][0]
    assert [m["to"] for m in emails] == ["kone@example.ci"] and templates == []  # un seul rappel ; jamais de modèle payant
    await engine.dispose()


# --- Alerte au restaurant 45 min avant, réservations à confirmer ---------------------------------------------------------

@pytest.mark.asyncio
async def test_restaurant_counts_tables_to_confirm_and_orders_to_start(db_session):
    tenant = await _resto(db_session)
    customer, conversation = await _conversation(db_session, tenant)
    now = datetime.now(timezone.utc)
    for minutes in (30, 60, -20):
        db_session.add(Order(tenant_id=tenant.id, customer_id=customer.id, status=OrderStatus.PENDING, total_amount=1,
                             currency="XOF", fulfillment="PICKUP", created_by="IA", scheduled_for=now + timedelta(minutes=minutes)))
    started = Order(tenant_id=tenant.id, customer_id=customer.id, status=OrderStatus.PENDING, total_amount=1, currency="XOF",
                    fulfillment="PICKUP", created_by="IA", scheduled_for=now + timedelta(minutes=10))
    db_session.add(started)
    await db_session.flush()
    db_session.add(Delivery(tenant_id=tenant.id, order_id=started.id, status=DeliveryStatus.READY))
    db_session.add(AppointmentRequest(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id,
                                      kind="TABLE", status=STATUS_REQUESTED, party_size=12,
                                      availability="Samedi soir"))
    await db_session.commit()
    counts = await notifications.task_counts(db_session, tenant)
    assert counts["kitchen"] == 2  # dans 30 min, et en retard de 20 min ; pas celle d'une heure, ni celle déjà prête
    assert counts["appointments"] == 1 and counts["orders"] == 4
    assert notifications.TITLES["kitchen"] == "Commande pour plus tard à préparer"
    assert notifications.new_tasks({"kitchen": 1}, counts) == ["appointments", "orders", "kitchen"]


def test_dashboard_tables_ready_and_kitchen_alert():
    assert 'id="table-qr-card"' in HTML and "function createTableQrs()" in HTML and "loadTableQrs();" in HTML
    assert '["READY", "Prête"]' in HTML and 'mode === "DINE_IN" ? "Servie" : "Retirée"' in HTML
    assert 'orders: ["orders", "kitchen"]' in HTML and "kitchen: \"orders\"" in HTML
    assert 'res.notified_step === "READY" ? "commande prête"' in HTML


@pytest.mark.asyncio
async def test_a_table_qr_downloads_as_svg(client, db_session):
    tenant = await _resto(db_session)
    headers = await _headers(client, tenant)
    tables = (await client.post("/api/v1/table-qr-codes", headers=headers, json={"first": 1, "last": 1})).json()
    r = await client.get(f"/api/v1/table-qr-codes/{tables[0]['id']}/qr.svg", headers=headers)
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml") and "<svg" in r.text
    other = await _resto(db_session)
    assert (await client.get(f"/api/v1/table-qr-codes/{tables[0]['id']}/qr.svg",
                             headers=await _headers(client, other))).status_code == 404
    assert "function downloadTableQr(id, label)" in HTML
