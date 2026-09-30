"""Lot 35 — rendez-vous côté prospect : rappel WhatsApp si la conversation est ouverte, déplacer ou annuler."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agents.dependency import get_llm_client
from app.agents.tools import ToolExecutor
from app.core.database import Base
from app.core.security import hash_password
from app.main import app
from app.models.appointment_request import AppointmentRequest
from app.models.appointment_settings import TenantAppointmentSettings
from app.models.contact_point import ContactPoint
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import booking
from app.services.business_type import CAR_DEALERSHIP, ONLINE_STORE
from app.services.local_time import tenant_zone
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response

ALL_DAY = {str(d): [["06:00", "22:00"]] for d in range(7)}


async def _dealer(db_session, email, booking_on=True, phone_number_id=None):
    tenant = Tenant(name="Auto Plus", country="SN", currency="XOF", email=email, plan=TenantPlan.PRO, business_type=CAR_DEALERSHIP)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    if phone_number_id:
        db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=phone_number_id, system_user_token="t"))
    db_session.add(TenantAppointmentSettings(tenant_id=tenant.id, online_booking=booking_on, opening_hours=ALL_DAY))
    await db_session.commit()
    return tenant


async def _prospect(db_session, tenant, number=None, **customer_kw):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=number or f"2217{uuid.uuid4().int % 10**8:08d}", **customer_kw)
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    return customer, conversation


async def _slots(db_session, tenant, limit=3):
    settings = await booking.load_settings(db_session, tenant.id)
    return await booking.free_slots(db_session, tenant, settings, tenant_zone(tenant), datetime.now(timezone.utc), limit=limit)


async def _booked(db_session, tenant, conversation):
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    [slot, *_] = await _slots(db_session, tenant)
    await executor.execute("request_appointment", {"kind": "ESSAI", "slot": booking.slot_id(slot), "vehicle": "Peugeot 5008"})
    await db_session.commit()
    return (await db_session.execute(select(AppointmentRequest).where(
        AppointmentRequest.conversation_id == conversation.id))).scalar_one()


# --- Retrouver son rendez-vous ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_prospect_only_sees_his_own_upcoming_appointments(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _prospect(db_session, tenant)
    other, other_conversation = await _prospect(db_session, tenant)
    mine = await _booked(db_session, tenant, conversation)
    await _booked(db_session, tenant, other_conversation)
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id, kind="VISITE",
                                      availability="x", status="CONFIRMED", scheduled_at=datetime.now(timezone.utc) - timedelta(days=2)))
    await db_session.commit()

    result = await ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP).execute("get_my_appointments", {})

    assert [a["appointment_id"] for a in result["appointments"]] == [str(mine.id)]
    assert result["appointments"][0]["vehicle"] == "Peugeot 5008" and result["appointments"][0]["status"] == "confirmé"


# --- Déplacer --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prospect_moves_his_appointment_to_a_free_slot(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _prospect(db_session, tenant)
    appointment = await _booked(db_session, tenant, conversation)
    appointment.reminder_sent_at = appointment.customer_whatsapp_reminder_sent_at = datetime.now(timezone.utc)
    previous = appointment.scheduled_at
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    new_slot = (await executor.execute("get_available_slots", {}))["slots"][1]

    result = await executor.execute("reschedule_my_appointment", {"appointment_id": str(appointment.id), "slot": new_slot["slot"]})

    assert result["status"] == "rescheduled" and result["when"] == new_slot["label"]
    assert appointment.scheduled_at != previous and appointment.rescheduled_by == "CLIENT"
    assert appointment.reminder_sent_at is None and appointment.customer_whatsapp_reminder_sent_at is None
    assert executor.message_outbox == [f"C'est noté : votre essai (Peugeot 5008) est déplacé au {new_slot['label']}. À bientôt chez Auto Plus !"]
    assert executor.change_outbox == [(appointment, previous)]


@pytest.mark.asyncio
async def test_moving_to_its_own_slot_is_not_blocked_by_itself(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _prospect(db_session, tenant)
    appointment = await _booked(db_session, tenant, conversation)
    own = booking.slot_id(appointment.scheduled_at.replace(tzinfo=timezone.utc).astimezone(tenant_zone(tenant)))
    result = await ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP).execute(
        "reschedule_my_appointment", {"appointment_id": str(appointment.id), "slot": own})
    assert result["status"] == "rescheduled"


@pytest.mark.asyncio
async def test_cannot_move_to_a_taken_slot_nor_touch_someone_else(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _prospect(db_session, tenant)
    other, other_conversation = await _prospect(db_session, tenant)
    mine = await _booked(db_session, tenant, conversation)
    theirs = await _booked(db_session, tenant, other_conversation)
    taken = booking.slot_id(theirs.scheduled_at.replace(tzinfo=timezone.utc).astimezone(tenant_zone(tenant)))
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)

    busy = await executor.execute("reschedule_my_appointment", {"appointment_id": str(mine.id), "slot": taken})
    assert "pas disponible" in busy["error"] and busy["available_slots"]
    foreign = await executor.execute("reschedule_my_appointment", {"appointment_id": str(theirs.id), "slot": taken})
    assert "introuvable" in foreign["error"]
    cancel_foreign = await executor.execute("cancel_my_appointment", {"appointment_id": str(theirs.id)})
    assert "introuvable" in cancel_foreign["error"] and theirs.status == "CONFIRMED"
    assert "introuvable" in (await executor.execute("cancel_my_appointment", {"appointment_id": "pas-un-id"}))["error"]
    assert executor.message_outbox == [] and executor.change_outbox == []


@pytest.mark.asyncio
async def test_without_online_slots_moving_goes_to_an_advisor(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _prospect(db_session, tenant)
    appointment = await _booked(db_session, tenant, conversation)
    settings = await booking.load_settings(db_session, tenant.id)
    settings.online_booking = False
    await db_session.commit()
    result = await ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP).execute(
        "reschedule_my_appointment", {"appointment_id": str(appointment.id), "slot": "2030-01-01T10:00"})
    assert "handoff_to_human" in result["error"]


# --- Annuler ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prospect_cancels_his_appointment(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _prospect(db_session, tenant)
    appointment = await _booked(db_session, tenant, conversation)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)

    result = await executor.execute("cancel_my_appointment", {"appointment_id": str(appointment.id)})

    assert result["status"] == "cancelled"
    assert (appointment.status, appointment.cancelled_by) == ("CANCELLED", "CLIENT") and appointment.cancelled_at
    assert executor.message_outbox[0].startswith("Bonjour, votre rendez-vous du ") and "est annulé" in executor.message_outbox[0]
    again = await executor.execute("cancel_my_appointment", {"appointment_id": str(appointment.id)})
    assert "introuvable" in again["error"]


@pytest.mark.asyncio
async def test_online_store_has_none_of_these_tools(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _prospect(db_session, tenant)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=ONLINE_STORE)
    for tool in ("get_my_appointments", "reschedule_my_appointment", "cancel_my_appointment"):
        assert "non disponible" in (await executor.execute(tool, {"appointment_id": "x", "slot": "x"}))["error"]


@pytest.mark.asyncio
async def test_cancel_by_whatsapp_confirms_and_alerts_shop_and_commercial(client, db_session, unique_email, monkeypatch):
    sent, mails = [], []

    class _Recorder:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, to, body):
            sent.append(body)
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Recorder)
    monkeypatch.setattr("app.api.webhooks.whatsapp.send_email", lambda **kw: mails.append(kw) or True)
    tenant = await _dealer(db_session, unique_email, phone_number_id="pn-rdv-1")
    cp = ContactPoint(tenant_id=tenant.id, code="m35", name="Moussa", greeting="B", owner_name="Moussa", owner_email="moussa@example.com")
    db_session.add(cp)
    await db_session.flush()
    customer, conversation = await _prospect(db_session, tenant, referred_contact_point_id=cp.id, email="deja@example.com")
    appointment = await _booked(db_session, tenant, conversation)
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([
        tool_use_response("cancel_my_appointment", {"appointment_id": str(appointment.id)}),
        text_response("C'est fait."),
    ])
    try:
        await client.post("/webhooks/whatsapp", json={"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "pn-rdv-1"},
            "messages": [{"from": customer.whatsapp_number, "id": f"wamid.{uuid.uuid4().hex}", "type": "text",
                          "text": {"body": "Annulez mon rendez-vous svp"}, "timestamp": "1"}]}}]}]})
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    assert sent[0] == "C'est fait." and "est annulé" in sent[1] and len(sent) == 2
    stored = (await db_session.execute(select(Message.message_type).where(Message.content == sent[1]))).scalars().all()
    assert stored == ["auto_message"]
    assert sorted(m["to"] for m in mails) == sorted([unique_email, "moussa@example.com"])
    assert all(m["subject"].startswith("Rendez-vous annulé par le client") for m in mails)


# --- Rappel WhatsApp la veille ----------------------------------------------------------------

async def _eve_setup(last_customer_message_hours_ago):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    evening = datetime(2026, 10, 2, 18, 30, tzinfo=timezone.utc)  # 18 h 30 à Dakar, la veille
    async with factory() as db:
        tenant = Tenant(name="Auto Plus", country="SN", currency="XOF", email="concession@example.com",
                        plan=TenantPlan.PRO, business_type=CAR_DEALERSHIP)
        db.add(tenant)
        await db.flush()
        customer = Customer(tenant_id=tenant.id, whatsapp_number="221700009999")
        db.add(customer)
        await db.flush()
        conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id)
        db.add(conversation)
        await db.flush()
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER, message_type="text",
                       content="Merci !", created_at=evening - timedelta(hours=last_customer_message_hours_ago)))
        db.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id, kind="ESSAI",
                                  vehicle_label="Peugeot 5008", availability="x", status="CONFIRMED",
                                  scheduled_at=datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)))
        await db.commit()
    return engine, factory, evening


@pytest.mark.asyncio
async def test_whatsapp_reminder_only_when_the_conversation_is_open():
    from app.workers import appointment_reminders

    for hours_ago, expected in ((3, 1), (30, 0)):
        engine, factory, evening = await _eve_setup(hours_ago)
        whatsapp = []

        async def fake_whatsapp(db, tenant, conversation, customer, text):
            whatsapp.append(text)
            return True

        first = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening,
                                                               send=lambda **kw: True, send_whatsapp=fake_whatsapp)
        second = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening + timedelta(minutes=15),
                                                                send=lambda **kw: True, send_whatsapp=fake_whatsapp)
        assert first["whatsapp"] == expected and second["whatsapp"] == 0 and first["staff"] == 1
        assert len(whatsapp) == expected  # jamais deux fois ; rien du tout si la conversation est fermée
        if expected:
            assert whatsapp[0] == ("Bonjour ! Petit rappel : votre essai (Peugeot 5008) chez Auto Plus est prévu demain, "
                                   "samedi 3 octobre à 10 h. Pour le déplacer ou l'annuler, répondez simplement à ce message.")
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_failed_whatsapp_reminder_is_retried_later():
    from app.workers import appointment_reminders

    engine, factory, evening = await _eve_setup(2)

    async def refused(*a):
        return False

    async def accepted(*a):
        return True

    failed = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening, send=lambda **kw: True, send_whatsapp=refused)
    retried = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening, send=lambda **kw: True, send_whatsapp=accepted)
    assert failed["whatsapp"] == 0 and failed["failed"] == 1 and retried["whatsapp"] == 1
    await engine.dispose()


# --- Page Rendez-vous ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_appointments_page_shows_changes_made_by_the_prospect(client, db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _prospect(db_session, tenant)
    appointment = await _booked(db_session, tenant, conversation)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    new_slot = (await executor.execute("get_available_slots", {}))["slots"][1]["slot"]
    await executor.execute("reschedule_my_appointment", {"appointment_id": str(appointment.id), "slot": new_slot})
    await db_session.commit()
    token = (await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})).json()["access_token"]

    items = (await client.get("/api/v1/appointments?view=confirmed", headers={"Authorization": f"Bearer {token}"})).json()["items"]

    assert items[0]["customer_change"] == "Déplacé par le client" and items[0]["whatsapp_reminder_sent_at"] is None


def test_dashboard_shows_prospect_changes_escaped():
    html = open("app/static/dashboard/index.html", encoding="utf-8").read()
    assert "${esc(a.customer_change)}" in html and "Rappel WhatsApp envoyé" in html


# --- Lot 35b/35c : créneaux proposés d'office, annulation seulement si elle est explicite -----

@pytest.mark.asyncio
async def test_finding_the_appointment_gives_free_slots_right_away(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _prospect(db_session, tenant)
    await _booked(db_session, tenant, conversation)
    result = await ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP).execute("get_my_appointments", {})
    assert len(result["available_slots"]) == 3 and "propose-lui TOUT DE SUITE" in result["instruction"]


@pytest.mark.parametrize("text, explicit", [
    ("Je ne veux plus venir lundi", False), ("Je ne peux plus venir lundi, on peut décaler ?", False), ("oui", False),
    ("Annulez mon rendez-vous", True), ("je ne viendrai pas du tout", True), ("laissez tomber", True),
    ("plus besoin, merci", True), ("supprimez le rdv", True),
])
def test_what_counts_as_an_explicit_cancellation(text, explicit):
    from app.agents.tools import asks_explicit_cancellation

    assert asks_explicit_cancellation(text) is explicit


@pytest.mark.asyncio
async def test_not_coming_on_monday_is_a_move_not_a_cancellation(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _prospect(db_session, tenant)
    appointment = await _booked(db_session, tenant, conversation)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    executor.incoming_text = "Je ne veux plus venir lundi"

    result = await executor.execute("cancel_my_appointment", {"appointment_id": str(appointment.id)})

    assert "ne l'annule pas" in result["error"] and len(result["available_slots"]) == 3
    assert appointment.status == "CONFIRMED" and executor.message_outbox == []


@pytest.mark.asyncio
async def test_yes_to_a_proposed_cancellation_cancels(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _prospect(db_session, tenant)
    appointment = await _booked(db_session, tenant, conversation)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    executor.incoming_text = "oui"
    blocked = await executor.execute("cancel_my_appointment", {"appointment_id": str(appointment.id)})
    assert "error" in blocked  # « oui » sans question d'annulation juste avant : pas une annulation

    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.AI, message_type="text",
                           content="Je peux vous proposer mardi 10 h. Préférez-vous plutôt annuler ?"))
    await db_session.commit()
    done = await executor.execute("cancel_my_appointment", {"appointment_id": str(appointment.id)})
    assert done["status"] == "cancelled" and appointment.status == "CANCELLED"


@pytest.mark.asyncio
async def test_other_answer_after_a_cancellation_question_is_not_a_yes(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _prospect(db_session, tenant)
    appointment = await _booked(db_session, tenant, conversation)
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.AI, message_type="text",
                           content="Préférez-vous déplacer ou annuler ?"))
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    executor.incoming_text = "mardi plutôt"
    assert "error" in await executor.execute("cancel_my_appointment", {"appointment_id": str(appointment.id)})
    assert appointment.status == "CONFIRMED"


@pytest.mark.asyncio
async def test_through_whatsapp_bob_cannot_cancel_without_an_explicit_request(client, db_session, unique_email, monkeypatch):
    class _Silent:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, *a, **kw):
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Silent)
    monkeypatch.setattr("app.api.webhooks.whatsapp.send_email", lambda **kw: True)
    tenant = await _dealer(db_session, unique_email, phone_number_id="pn-rdv-2")
    customer, conversation = await _prospect(db_session, tenant, email="deja@example.com")
    appointment = await _booked(db_session, tenant, conversation)
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([
        tool_use_response("cancel_my_appointment", {"appointment_id": str(appointment.id)}),
        text_response("Je peux vous proposer d'autres créneaux."),
    ])
    try:
        await client.post("/webhooks/whatsapp", json={"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "pn-rdv-2"},
            "messages": [{"from": customer.whatsapp_number, "id": f"wamid.{uuid.uuid4().hex}", "type": "text",
                          "text": {"body": "Je ne veux plus venir lundi"}, "timestamp": "1"}]}}]}]})
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    await db_session.refresh(appointment)
    assert appointment.status == "CONFIRMED"
