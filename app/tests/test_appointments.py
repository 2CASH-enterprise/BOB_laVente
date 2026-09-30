"""
Lot 25 — rendez-vous de concession : fiche de qualification, confirmation par un humain,
messages fixes au client, rappels par email la veille à 18 h (heure de la boutique).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agents.prompts import DEALERSHIP_RULES
from app.agents.tools import ToolExecutor
from app.core.database import Base
from app.core.security import hash_password
from app.integrations.whatsapp.client import WhatsAppSendError
from app.models.appointment_request import AppointmentRequest
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.messaging_settings import KillSwitch, OutboundMode, TenantMessagingSettings
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import appointment_service
from app.services.business_type import CAR_DEALERSHIP
from app.services.local_time import format_local, local_to_utc, tenant_zone

PARIS = tenant_zone(Tenant(country="FR"))
DAKAR = tenant_zone(Tenant(country="SN"))


# --- Heure de la boutique ---------------------------------------------------------------------

def test_local_time_follows_the_shop_country():
    summer = local_to_utc(datetime(2026, 10, 3, 10, 0), PARIS)
    assert summer == datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)  # heure d'été
    winter = local_to_utc(datetime(2026, 12, 5, 10, 0), PARIS)
    assert winter == datetime(2026, 12, 5, 9, 0, tzinfo=timezone.utc)
    assert local_to_utc(datetime(2026, 10, 3, 10, 0), DAKAR) == datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)
    assert tenant_zone(Tenant(country="ZZ")).key == "UTC"


def test_dates_are_written_in_french():
    assert format_local(datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc), PARIS) == "samedi 3 octobre à 10 h"
    assert format_local(datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc), DAKAR) == "dimanche 1er novembre à 9 h 30"


# --- Messages fixes -------------------------------------------------------------------------

def _appt(kind="ESSAI", vehicle="Peugeot 3008", scheduled=datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc), **kw):
    return AppointmentRequest(kind=kind, vehicle_label=vehicle, availability="samedi", scheduled_at=scheduled,
                              status=kw.pop("status", "CONFIRMED"), **kw)


def test_confirmation_messages_are_fixed_and_grammatical():
    assert appointment_service.confirmation_message(_appt(), "Auto Plus", PARIS) == (
        "Bonjour ! Votre essai (Peugeot 3008) est confirmé le samedi 3 octobre à 10 h. À bientôt chez Auto Plus !")
    assert "Votre visite est confirmée le" in appointment_service.confirmation_message(_appt("VISITE", None), "A", PARIS)
    assert "rendez-vous d'estimation de reprise est confirmé" in appointment_service.confirmation_message(
        _appt("ESTIMATION_REPRISE"), "A", PARIS)


def test_cancellation_message_with_and_without_date():
    assert "votre rendez-vous du samedi 3 octobre à 10 h est annulé" in appointment_service.cancellation_message(_appt(), "A", PARIS)
    assert "votre demande de rendez-vous est annulée" in appointment_service.cancellation_message(
        _appt(scheduled=None, status="REQUESTED"), "A", PARIS)


# --- Rappels : la veille à partir de 18 h, heure de la boutique -------------------------------

SAT_10H_PARIS = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc), True),    # vendredi 18 h à Paris
    (datetime(2026, 10, 2, 15, 59, tzinfo=timezone.utc), False),  # vendredi 17 h 59
    (datetime(2026, 10, 2, 21, 30, tzinfo=timezone.utc), True),   # vendredi 23 h 30 (confirmé tard)
    (datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc), False),   # jeudi : trop tôt
    (datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc), False),    # le jour même
])
def test_staff_reminder_is_due_the_evening_before(now, expected):
    assert appointment_service.reminder_due(_appt(scheduled=SAT_10H_PARIS), now, PARIS, None).staff is expected


def test_reminder_uses_the_shop_timezone_not_the_server_one():
    # 16 h 30 UTC : 18 h 30 à Paris (rappel dû), 16 h 30 à Dakar (pas encore).
    now = datetime(2026, 10, 2, 16, 30, tzinfo=timezone.utc)
    dakar_appt = _appt(scheduled=datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc))
    assert appointment_service.reminder_due(_appt(scheduled=SAT_10H_PARIS), now, PARIS, None).staff
    assert not appointment_service.reminder_due(dakar_appt, now, DAKAR, None).staff


def test_reminders_are_sent_once_only_for_confirmed_and_customer_needs_an_email():
    now = datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc)
    assert appointment_service.reminder_due(_appt(), now, PARIS, "client@example.com").customer
    assert not appointment_service.reminder_due(_appt(), now, PARIS, None).customer
    assert not appointment_service.reminder_due(_appt(reminder_sent_at=now), now, PARIS, None).staff
    assert not appointment_service.reminder_due(
        _appt(customer_reminder_sent_at=now), now, PARIS, "client@example.com").customer
    assert not appointment_service.reminder_due(_appt(status="CANCELLED"), now, PARIS, "c@example.com").staff
    assert not appointment_service.reminder_due(_appt(status="REQUESTED", scheduled=None), now, PARIS, None).staff


def test_rescheduling_resets_the_reminders():
    appt = _appt(reminder_sent_at=SAT_10H_PARIS, customer_reminder_sent_at=SAT_10H_PARIS)
    appointment_service.confirm(appt, SAT_10H_PARIS + timedelta(days=1), "u1")
    assert appt.reminder_sent_at is None and appt.customer_reminder_sent_at is None
    same = _appt(reminder_sent_at=SAT_10H_PARIS)
    appointment_service.confirm(same, SAT_10H_PARIS, "u1")  # même date : rien à renvoyer
    assert same.reminder_sent_at == SAT_10H_PARIS


# --- Bob remplit la fiche de qualification ----------------------------------------------------

async def _shop(db_session, email, country="FR", role=Role.OWNER, customer_age=timedelta(hours=1), account=True):
    tenant = Tenant(name="Auto Plus", country=country, currency="EUR", email=email, plan=TenantPlan.PRO,
                    business_type=CAR_DEALERSHIP)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role))
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"33{uuid.uuid4().int % 10**9:09d}", first_name="Awa")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.WAITING_HUMAN)
    db_session.add(conversation)
    await db_session.flush()
    if customer_age is not None:
        db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                               content="Samedi 10 h", created_at=datetime.now(timezone.utc) - customer_age))
    if account:
        db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=f"pn_{uuid.uuid4().hex[:10]}",
                                       system_user_token="t"))
    appt = AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                              kind="ESSAI", vehicle_label="Peugeot 3008", availability="samedi matin",
                              need="voiture familiale", financing_interest=True)
    db_session.add(appt)
    await db_session.commit()
    return tenant, conversation, customer, appt


@pytest.mark.asyncio
async def test_request_appointment_fills_the_qualification_sheet(db_session, unique_email):
    tenant, conversation, customer, _ = await _shop(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)

    await executor.execute("request_appointment", {
        "kind": "ESSAI", "availability": "mardi 18 h", "vehicle": "3008",
        "need": "voiture familiale", "budget": "25 000 €", "trade_in": "Clio 2017, 90 000 km",
        "financing_interest": True,
    })

    appt = (await db_session.execute(select(AppointmentRequest).where(
        AppointmentRequest.availability == "mardi 18 h"))).scalar_one()
    assert (appt.need, appt.budget, appt.trade_in, appt.financing_interest) == (
        "voiture familiale", "25 000 €", "Clio 2017, 90 000 km", True)
    handoff = (await db_session.execute(select(Message.content).where(
        Message.conversation_id == conversation.id, Message.message_type == "handoff"))).scalar_one()
    assert "besoin : voiture familiale ; budget : 25 000 € ; reprise : Clio 2017, 90 000 km ; financement : intéressé" in handoff


@pytest.mark.asyncio
async def test_financing_interest_must_be_a_real_yes_or_no(db_session, unique_email):
    tenant, conversation, _, _ = await _shop(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    await executor.execute("request_appointment", {"kind": "VISITE", "availability": "lundi", "financing_interest": "peut-être"})
    appt = (await db_session.execute(select(AppointmentRequest).where(AppointmentRequest.availability == "lundi"))).scalar_one()
    assert appt.financing_interest is None


def test_dealership_always_uses_vous():
    assert "Vouvoie TOUJOURS" in DEALERSHIP_RULES


# --- API : lister, confirmer, annuler --------------------------------------------------------

class _FakeWhatsApp:
    sent: list = []
    fail: Exception | None = None

    def __init__(self, phone_number_id, system_user_token):
        pass

    async def send_text_message(self, to, body):
        if _FakeWhatsApp.fail:
            raise _FakeWhatsApp.fail
        _FakeWhatsApp.sent.append({"to": to, "body": body})
        return {"messages": [{"id": "wamid.X"}]}


@pytest.fixture
def whatsapp(monkeypatch):
    import app.integrations.whatsapp.client as client_module

    _FakeWhatsApp.sent, _FakeWhatsApp.fail = [], None
    monkeypatch.setattr(client_module, "WhatsAppClient", _FakeWhatsApp)
    return _FakeWhatsApp


async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _next_saturday_10h_local() -> str:
    today = datetime.now(timezone.utc).date()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    return f"{saturday.isoformat()}T10:00"


@pytest.mark.asyncio
async def test_confirm_in_shop_time_notifies_the_customer_with_a_fixed_message(client, db_session, unique_email, whatsapp):
    tenant, conversation, customer, appt = await _shop(db_session, unique_email)
    local = _next_saturday_10h_local()

    r = await client.post(f"/api/v1/appointments/{appt.id}/confirm",
                          json={"scheduled_local": local, "notify_customer": True}, headers=await _headers(client, unique_email))

    assert r.status_code == 200
    body = r.json()
    assert body["customer_notified"] is True and body["appointment"]["status"] == "CONFIRMED"
    expected_utc = local_to_utc(datetime.fromisoformat(local), PARIS)
    await db_session.refresh(appt)
    assert appt.scheduled_at.replace(tzinfo=timezone.utc) == expected_utc
    [sent] = whatsapp.sent
    assert sent["to"] == customer.whatsapp_number
    assert sent["body"].startswith("Bonjour ! Votre essai (Peugeot 3008) est confirmé le samedi") and "à 10 h" in sent["body"]
    stored = (await db_session.execute(select(Message.content).where(
        Message.message_type == "appointment_confirmed"))).scalars().all()
    assert stored == [sent["body"]]
    assert (await db_session.execute(select(AuditLog).where(AuditLog.action == "APPOINTMENT_CONFIRMED"))).scalar_one()


@pytest.mark.asyncio
async def test_confirm_without_notification_sends_nothing(client, db_session, unique_email, whatsapp):
    _, _, _, appt = await _shop(db_session, unique_email)
    r = await client.post(f"/api/v1/appointments/{appt.id}/confirm",
                          json={"scheduled_local": _next_saturday_10h_local(), "notify_customer": False},
                          headers=await _headers(client, unique_email))
    assert r.json()["customer_notified"] is None and whatsapp.sent == []


@pytest.mark.asyncio
async def test_after_20h_the_appointment_is_confirmed_but_the_customer_is_not_messaged(client, db_session, unique_email, whatsapp):
    _, _, _, appt = await _shop(db_session, unique_email, customer_age=timedelta(hours=21))

    r = await client.post(f"/api/v1/appointments/{appt.id}/confirm",
                          json={"scheduled_local": _next_saturday_10h_local(), "notify_customer": True},
                          headers=await _headers(client, unique_email))

    body = r.json()
    assert body["appointment"]["status"] == "CONFIRMED" and body["appointment"]["can_notify"] is False
    assert body["customer_notified"] is False and "20 h" in body["notify_error"] and "Appelez-le" in body["notify_error"]
    assert whatsapp.sent == []
    assert (await db_session.execute(select(Message).where(Message.message_type == "appointment_confirmed"))).scalars().all() == []


@pytest.mark.asyncio
async def test_meta_refusal_is_reported_and_nothing_is_stored(client, db_session, unique_email, whatsapp):
    _, _, _, appt = await _shop(db_session, unique_email)
    whatsapp.fail = WhatsAppSendError(400, "code 131047 : Re-engagement message")

    r = await client.post(f"/api/v1/appointments/{appt.id}/confirm",
                          json={"scheduled_local": _next_saturday_10h_local(), "notify_customer": True},
                          headers=await _headers(client, unique_email))

    assert r.json()["customer_notified"] is False and "131047" in r.json()["notify_error"]
    assert (await db_session.execute(select(Message).where(Message.message_type == "appointment_confirmed"))).scalars().all() == []


@pytest.mark.asyncio
async def test_kill_switch_still_blocks_the_confirmation_message(client, db_session, unique_email, whatsapp):
    tenant, _, _, appt = await _shop(db_session, unique_email)
    db_session.add(TenantMessagingSettings(tenant_id=tenant.id, outbound_mode=OutboundMode.AI_ONLY, kill_switch=KillSwitch.BLOCKED))
    await db_session.commit()

    r = await client.post(f"/api/v1/appointments/{appt.id}/confirm",
                          json={"scheduled_local": _next_saturday_10h_local(), "notify_customer": True},
                          headers=await _headers(client, unique_email))

    assert r.json()["customer_notified"] is False and whatsapp.sent == []


@pytest.mark.asyncio
async def test_confirm_refuses_a_past_date_a_cancelled_appointment_and_another_shop(client, db_session, unique_email, whatsapp):
    _, _, _, appt = await _shop(db_session, unique_email)
    _, _, _, foreign = await _shop(db_session, f"other_{unique_email}")
    headers = await _headers(client, unique_email)

    past = await client.post(f"/api/v1/appointments/{appt.id}/confirm",
                             json={"scheduled_local": "2020-01-01T10:00"}, headers=headers)
    other = await client.post(f"/api/v1/appointments/{foreign.id}/confirm",
                              json={"scheduled_local": _next_saturday_10h_local()}, headers=headers)
    await client.post(f"/api/v1/appointments/{appt.id}/cancel", json={}, headers=headers)
    cancelled = await client.post(f"/api/v1/appointments/{appt.id}/confirm",
                                  json={"scheduled_local": _next_saturday_10h_local()}, headers=headers)

    assert (past.status_code, other.status_code, cancelled.status_code) == (422, 404, 409)
    await db_session.refresh(foreign)
    assert foreign.status == "REQUESTED"


@pytest.mark.asyncio
async def test_a_viewer_cannot_confirm(client, db_session, unique_email, whatsapp):
    _, _, _, appt = await _shop(db_session, unique_email, role=Role.VIEWER)
    r = await client.post(f"/api/v1/appointments/{appt.id}/confirm",
                          json={"scheduled_local": _next_saturday_10h_local()}, headers=await _headers(client, unique_email))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_cancel_with_notification(client, db_session, unique_email, whatsapp):
    _, _, _, appt = await _shop(db_session, unique_email)
    r = await client.post(f"/api/v1/appointments/{appt.id}/cancel", json={"notify_customer": True},
                          headers=await _headers(client, unique_email))
    assert r.json()["appointment"]["status"] == "CANCELLED" and r.json()["customer_notified"] is True
    assert "votre demande de rendez-vous est annulée" in whatsapp.sent[0]["body"]


@pytest.mark.asyncio
async def test_views_split_pending_upcoming_and_closed_and_stay_in_the_shop(client, db_session, unique_email, whatsapp):
    tenant, conversation, customer, pending = await _shop(db_session, unique_email)
    now = datetime.now(timezone.utc)
    common = dict(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id, availability="x")
    upcoming = AppointmentRequest(kind="VISITE", status="CONFIRMED", scheduled_at=now + timedelta(days=2), **common)
    past = AppointmentRequest(kind="ESSAI", status="CONFIRMED", scheduled_at=now - timedelta(days=1), **common)
    cancelled = AppointmentRequest(kind="ESSAI", status="CANCELLED", **common)
    db_session.add_all([upcoming, past, cancelled])
    await db_session.commit()
    await _shop(db_session, f"other_{unique_email}")  # une autre concession avec sa propre demande
    headers = await _headers(client, unique_email)

    async def ids(view):
        data = (await client.get(f"/api/v1/appointments?view={view}", headers=headers)).json()
        return {item["id"] for item in data["items"]}, data

    pending_ids, data = await ids("pending")
    assert pending_ids == {str(pending.id)} and data["timezone"] == "Europe/Paris"
    assert data["items"][0]["need"] == "voiture familiale" and data["items"][0]["customer"].startswith("Awa")
    assert (await ids("confirmed"))[0] == {str(upcoming.id)}
    assert (await ids("closed"))[0] == {str(past.id), str(cancelled.id)}
    assert (await client.get("/api/v1/appointments?view=everything", headers=headers)).status_code == 422


# --- Rappels : tâche planifiée ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_reminder_task_emails_staff_and_customer_once():
    from app.workers import appointment_reminders

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        tenant = Tenant(name="Auto Plus", country="FR", currency="EUR", email="concession@example.com",
                        plan=TenantPlan.PRO, business_type=CAR_DEALERSHIP)
        db.add(tenant)
        await db.flush()
        with_email = Customer(tenant_id=tenant.id, whatsapp_number="33600000001", email="client@example.com", first_name="Awa")
        no_email = Customer(tenant_id=tenant.id, whatsapp_number="33600000002")
        db.add_all([with_email, no_email])
        await db.flush()
        convs = [Conversation(tenant_id=tenant.id, customer_id=c.id) for c in (with_email, no_email)]
        db.add_all(convs)
        await db.flush()
        for conv, cust in zip(convs, (with_email, no_email)):
            db.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conv.id, customer_id=cust.id, kind="ESSAI",
                                      vehicle_label="3008", availability="samedi", status="CONFIRMED",
                                      scheduled_at=SAT_10H_PARIS, need="familiale"))
        await db.commit()

    outbox = []

    def fake_send(to, subject, body, **kw):
        outbox.append({"to": to, "subject": subject, "body": body})
        return True

    evening = datetime(2026, 10, 2, 16, 30, tzinfo=timezone.utc)  # vendredi 18 h 30 à Paris
    first = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening, send=fake_send)
    second = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening + timedelta(minutes=15), send=fake_send)

    assert first == {"staff": 2, "customer": 1, "whatsapp": 0, "failed": 0}
    assert second == {"staff": 0, "customer": 0, "whatsapp": 0, "failed": 0}
    staff = [m for m in outbox if m["to"] == "concession@example.com"]
    assert len(staff) == 2 and staff[0]["subject"].startswith("Rappel : rendez-vous demain")
    assert "samedi 3 octobre à 10 h" in staff[0]["body"] and "Besoin : familiale" in staff[0]["body"]
    [customer_mail] = [m for m in outbox if m["to"] == "client@example.com"]
    assert "Rappel de votre rendez-vous chez Auto Plus" == customer_mail["subject"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_email_is_retried_at_the_next_run():
    from app.workers import appointment_reminders

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        tenant = Tenant(name="A", country="FR", currency="EUR", email="c@example.com", plan=TenantPlan.PRO)
        db.add(tenant)
        await db.flush()
        cust = Customer(tenant_id=tenant.id, whatsapp_number="33600000003")
        db.add(cust)
        await db.flush()
        conv = Conversation(tenant_id=tenant.id, customer_id=cust.id)
        db.add(conv)
        await db.flush()
        db.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conv.id, customer_id=cust.id, kind="VISITE",
                                  availability="x", status="CONFIRMED", scheduled_at=SAT_10H_PARIS))
        await db.commit()

    evening = datetime(2026, 10, 2, 16, 30, tzinfo=timezone.utc)
    failed = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening, send=lambda **kw: False)
    retried = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening, send=lambda **kw: True)
    assert failed["failed"] == 1 and retried["staff"] == 1
    await engine.dispose()


def test_reminder_task_is_registered_every_quarter_hour():
    from app.workers.celery_app import TASK_MODULES, celery_app

    assert "app.workers.appointment_reminders" in TASK_MODULES
    entry = celery_app.conf.beat_schedule["appointment-reminders"]
    assert entry["task"] == "app.workers.appointment_reminders.send_appointment_reminders_task"
    assert entry["schedule"].minute == {0, 15, 30, 45}


# --- Accueil ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_home_lists_appointments_to_confirm_without_duplicates(db_session, unique_email):
    from app.services.home_service import home_summary

    tenant, conversation, _, appt = await _shop(db_session, unique_email)
    waiting = await home_summary(db_session, tenant.id)
    kinds = [t["kind"] for t in waiting["todo"]]
    assert kinds.count("CONVERSATION") == 1 and "APPOINTMENT" not in kinds  # déjà listée comme conversation

    conversation.status = ConversationStatus.ACTIVE  # rendue à Bob : la demande reste à confirmer
    await db_session.commit()
    released = await home_summary(db_session, tenant.id)
    [item] = [t for t in released["todo"] if t["kind"] == "APPOINTMENT"]
    assert item["reason"] == "Rendez-vous à confirmer" and item["action"] == "Confirmer"
    assert "Essai · Peugeot 3008 · samedi matin" in item["detail"]


def test_appointment_transfer_reason_is_readable_on_home():
    from app.services.home_service import _reason

    transfer = Message(message_type="handoff", content="Transfert vers un humain : Rendez-vous à confirmer — Essai — 3008")
    assert _reason(transfer) == ("Rendez-vous à confirmer", "info")
