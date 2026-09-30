"""Lot 29 — créneaux de rendez-vous : calculés par le code, proposés par Bob, confirmés à la réservation."""
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.agents.dependency import get_llm_client
from app.agents.prompts import DEALERSHIP_RULES
from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.main import app
from app.models.appointment_request import AppointmentRequest
from app.models.appointment_settings import TenantAppointmentSettings
from app.models.contact_point import ContactPoint
from app.models.conversation import Conversation, ConversationStatus, Message
from app.models.customer import Customer
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import booking
from app.services.business_type import CAR_DEALERSHIP, ONLINE_STORE
from app.services.local_time import tenant_zone
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response

DAKAR = ZoneInfo("Africa/Dakar")
PARIS = ZoneInfo("Europe/Paris")
WEEK = {str(d): [["09:00", "12:00"], ["14:00", "18:00"]] for d in range(6)}  # lundi → samedi, fermé dimanche
ALL_DAY = {str(d): [["06:00", "22:00"]] for d in range(7)}


def _settings(hours=None, slot=60, capacity=1, enabled=True):
    return TenantAppointmentSettings(online_booking=enabled, opening_hours=hours if hours is not None else WEEK,
                                     slot_minutes=slot, capacity=capacity)


class _Tenant:
    id = uuid.uuid4()


class _NoDb:
    """free_slots sans base : aucun rendez-vous déjà pris."""

    def __init__(self, booked=()):
        self.booked = list(booked)

    async def execute(self, _stmt):
        booked = self.booked

        class _R:
            def scalars(self):
                class _S:
                    def all(self):
                        return booked
                return _S()
        return _R()


# Mercredi 30 septembre 2026, 8 h 30 à Dakar (UTC+0).
WED_0830 = datetime(2026, 9, 30, 8, 30, tzinfo=timezone.utc)


# --- Horaires ------------------------------------------------------------------------------

def test_opening_hours_are_validated_and_sorted():
    assert booking.validate_opening_hours({"5": [["14:00", "18:00"], ["09:00", "12:00"]], "6": []}) == {
        "5": [["09:00", "12:00"], ["14:00", "18:00"]]}


@pytest.mark.parametrize("hours", [
    {"0": [["25:00", "26:00"]]}, {"0": [["9h", "18h"]]}, {"0": [["18:00", "09:00"]]}, {"0": [["10:00", "10:00"]]},
    {"0": [["09:00", "13:00"], ["12:00", "18:00"]]}, {"0": [["08:00", "09:00"], ["10:00", "11:00"], ["12:00", "13:00"]]},
    {"7": [["09:00", "18:00"]]}, {"0": [["09:00"]]}, {"0": "09:00-18:00"}, "lundi",
])
def test_bad_opening_hours_rejected(hours):
    with pytest.raises(ValueError):
        booking.validate_opening_hours(hours)


# --- Calcul des créneaux --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_without_preference_bob_proposes_three_different_days():
    slots = await booking.free_slots(_NoDb(), _Tenant(), _settings(), DAKAR, WED_0830)
    # Mercredi : 8 h 30 + 2 h de délai → 11 h au plus tôt ; puis jeudi et vendredi à l'ouverture.
    assert [booking.slot_id(s) for s in slots] == ["2026-09-30T11:00", "2026-10-01T09:00", "2026-10-02T09:00"]


@pytest.mark.asyncio
async def test_preferred_day_and_part_of_day():
    saturday = date(2026, 10, 3)
    morning = await booking.free_slots(_NoDb(), _Tenant(), _settings(), DAKAR, WED_0830, day=saturday, part="MATIN")
    afternoon = await booking.free_slots(_NoDb(), _Tenant(), _settings(), DAKAR, WED_0830, day=saturday, part="APRES_MIDI")
    assert [s.hour for s in morning] == [9, 10, 11]
    assert [s.hour for s in afternoon] == [14, 15, 16]


@pytest.mark.asyncio
async def test_closed_day_and_beyond_seven_days_give_nothing():
    sunday = date(2026, 10, 4)
    too_far = date(2026, 10, 8)
    assert await booking.free_slots(_NoDb(), _Tenant(), _settings(), DAKAR, WED_0830, day=sunday) == []
    assert await booking.free_slots(_NoDb(), _Tenant(), _settings(), DAKAR, WED_0830, day=too_far) == []
    assert await booking.free_slots(_NoDb(), _Tenant(), _settings(), DAKAR, WED_0830, day=date(2026, 10, 7)) != []


@pytest.mark.asyncio
async def test_slot_must_fit_before_closing():
    saturday = date(2026, 10, 3)
    slots = await booking.free_slots(_NoDb(), _Tenant(), _settings({"5": [["09:00", "11:30"]]}, slot=60), DAKAR, WED_0830,
                                     day=saturday, limit=10)
    assert [s.strftime("%H:%M") for s in slots] == ["09:00", "10:00"]  # 11 h finirait à 12 h, après la fermeture


@pytest.mark.asyncio
async def test_full_slot_is_skipped_and_capacity_counts():
    saturday = date(2026, 10, 3)
    taken = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)
    one = await booking.free_slots(_NoDb([taken]), _Tenant(), _settings(), DAKAR, WED_0830, day=saturday, part="MATIN")
    two = await booking.free_slots(_NoDb([taken]), _Tenant(), _settings(capacity=2), DAKAR, WED_0830, day=saturday, part="MATIN")
    assert [s.hour for s in one] == [9, 11]
    assert [s.hour for s in two] == [9, 10, 11]


@pytest.mark.asyncio
async def test_an_appointment_at_an_odd_time_blocks_both_overlapping_slots():
    saturday = date(2026, 10, 3)
    odd = datetime(2026, 10, 3, 9, 30, tzinfo=timezone.utc)  # fixé à la main par un conseiller
    slots = await booking.free_slots(_NoDb([odd]), _Tenant(), _settings(), DAKAR, WED_0830, day=saturday, part="MATIN")
    assert [s.hour for s in slots] == [11]


@pytest.mark.asyncio
async def test_times_are_the_shops_local_time():
    slots = await booking.free_slots(_NoDb(), _Tenant(), _settings(), PARIS, WED_0830, day=date(2026, 10, 3), part="MATIN")
    assert slots[0].astimezone(timezone.utc).hour == 7  # 9 h à Paris en heure d'été = 7 h UTC
    assert booking.slots_for_ai(slots[:1], PARIS) == [{"slot": "2026-10-03T09:00", "label": "samedi 3 octobre à 9 h"}]


def test_today_is_given_to_bob_in_the_shops_time():
    late = datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)  # 1 h 30 le 1er octobre à Paris
    assert booking.today_for_ai(late, PARIS) == {"today": "jeudi 1 octobre 2026 (2026-10-01)"}
    assert booking.today_for_ai(late, DAKAR) == {"today": "mercredi 30 septembre 2026 (2026-09-30)"}


def test_slot_ids_are_strict():
    assert booking.parse_slot_id("2026-10-03T10:00", DAKAR) == datetime(2026, 10, 3, 10, 0, tzinfo=DAKAR)
    for bad in ("samedi 10h", "2026-10-03 10:00", "2026-10-03T10:00:00", None, 42):
        with pytest.raises(ValueError):
            booking.parse_slot_id(bad, DAKAR)


# --- Outils de Bob --------------------------------------------------------------------------

async def _dealer(db_session, email, hours=None, enabled=True, capacity=1, country="SN", phone_number_id=None):
    tenant = Tenant(name="Auto Plus", country=country, currency="XOF", email=email, plan=TenantPlan.PRO, business_type=CAR_DEALERSHIP)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    if phone_number_id:
        db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=phone_number_id, system_user_token="t"))
    if hours is not False:
        db_session.add(TenantAppointmentSettings(tenant_id=tenant.id, online_booking=enabled,
                                                 opening_hours=hours or ALL_DAY, slot_minutes=60, capacity=capacity))
    await db_session.commit()
    return tenant


async def _conversation(db_session, tenant):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    return conversation


@pytest.mark.asyncio
async def test_bob_books_a_slot_and_it_is_confirmed(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    conversation = await _conversation(db_session, tenant)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)

    offered = await executor.execute("get_available_slots", {})
    assert len(offered["slots"]) == 3 and "today" in offered
    chosen = offered["slots"][1]
    result = await executor.execute("request_appointment", {"kind": "ESSAI", "slot": chosen["slot"], "vehicle": "Peugeot 5008", "need": "familiale"})

    assert result["status"] == "appointment_confirmed" and result["when"] == chosen["label"]
    [appointment] = (await db_session.execute(select(AppointmentRequest))).scalars().all()
    assert (appointment.status, appointment.confirmed_by, appointment.availability) == ("CONFIRMED", "BOB", chosen["label"])
    local = appointment.scheduled_at.replace(tzinfo=timezone.utc).astimezone(tenant_zone(tenant))
    assert local.strftime("%Y-%m-%dT%H:%M") == chosen["slot"]
    assert conversation.status == ConversationStatus.ACTIVE  # pas de transfert : Bob garde la main
    assert executor.booking_outbox == [appointment]
    [note] = (await db_session.execute(select(Message.content).where(Message.message_type == "appointment_booked"))).scalars().all()
    assert note.startswith("Rendez-vous confirmé par Bob : Essai — Peugeot 5008 — ")


@pytest.mark.asyncio
async def test_a_taken_slot_is_refused_with_fresh_slots(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    first = ToolExecutor(db_session, tenant.id, await _conversation(db_session, tenant), business_type=CAR_DEALERSHIP)
    slot = (await first.execute("get_available_slots", {}))["slots"][0]["slot"]
    assert (await first.execute("request_appointment", {"kind": "VISITE", "slot": slot}))["status"] == "appointment_confirmed"

    second = ToolExecutor(db_session, tenant.id, await _conversation(db_session, tenant), business_type=CAR_DEALERSHIP)
    result = await second.execute("request_appointment", {"kind": "VISITE", "slot": slot})

    assert "pas disponible" in result["error"] and slot not in [s["slot"] for s in result["available_slots"]]
    assert second.booking_outbox == []
    assert len((await db_session.execute(select(AppointmentRequest))).scalars().all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["2020-01-01T10:00", "2026-13-40T10:00", "samedi 10 h"])
async def test_invented_or_past_slot_never_booked(db_session, unique_email, slot):
    tenant = await _dealer(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, await _conversation(db_session, tenant), business_type=CAR_DEALERSHIP)
    result = await executor.execute("request_appointment", {"kind": "ESSAI", "slot": slot})
    assert "error" in result
    assert (await db_session.execute(select(AppointmentRequest))).scalars().all() == []


@pytest.mark.asyncio
async def test_misaligned_slot_never_booked(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, await _conversation(db_session, tenant), business_type=CAR_DEALERSHIP)
    good = (await executor.execute("get_available_slots", {}))["slots"][0]["slot"]
    odd = good[:-2] + "17"  # 10 h 17 : pas un début de créneau
    assert "error" in await executor.execute("request_appointment", {"kind": "ESSAI", "slot": odd})


@pytest.mark.asyncio
async def test_without_online_booking_the_old_flow_remains(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email, enabled=False)
    conversation = await _conversation(db_session, tenant)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)

    assert (await executor.execute("get_available_slots", {}))["status"] == "no_online_booking"
    assert "error" in await executor.execute("request_appointment", {"kind": "ESSAI", "slot": "2026-10-03T10:00"})
    result = await executor.execute("request_appointment", {"kind": "ESSAI", "availability": "samedi matin"})
    assert result["status"] == "appointment_requested" and conversation.status == ConversationStatus.WAITING_HUMAN


@pytest.mark.asyncio
async def test_preferred_day_that_is_closed_falls_back_with_a_note(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email, hours={str(d): [["09:00", "18:00"]] for d in range(1, 6)})
    executor = ToolExecutor(db_session, tenant.id, await _conversation(db_session, tenant), business_type=CAR_DEALERSHIP)
    today = datetime.now(tenant_zone(tenant)).date()
    next_sunday = today + timedelta(days=(6 - today.weekday()) or 7)
    result = await executor.execute("get_available_slots", {"preferred_date": next_sunday.isoformat()})
    assert "note" in result and result["slots"]
    assert "error" in await executor.execute("get_available_slots", {"preferred_date": "samedi"})


@pytest.mark.asyncio
async def test_online_store_never_gets_slots(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, await _conversation(db_session, tenant), business_type=ONLINE_STORE)
    assert "non disponible" in (await executor.execute("get_available_slots", {}))["error"]


def test_prompt_forbids_slots_not_coming_from_the_tool():
    assert "get_available_slots" in DEALERSHIP_RULES and "jamais un autre" in DEALERSHIP_RULES


# --- Webhook : confirmation fixe au client et alertes --------------------------------------

@pytest.fixture
def wa(monkeypatch):
    sent = []

    class _Recorder:
        fail_on = None

        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, to, body):
            if _Recorder.fail_on and _Recorder.fail_on in body:
                raise RuntimeError("Meta indisponible")
            sent.append(body)
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Recorder)
    return sent, _Recorder


@pytest.fixture
def outbox(monkeypatch):
    mails = []
    monkeypatch.setattr("app.api.webhooks.whatsapp.send_email", lambda **kw: mails.append(kw) or True)
    return mails


@pytest.fixture
def llm():
    def use(responses):
        app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient(responses)

    yield use
    app.dependency_overrides.pop(get_llm_client, None)


def _payload(phone_number_id, number, text):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": phone_number_id}, "messages": [
        {"from": number, "id": f"wamid.{uuid.uuid4().hex}", "type": "text", "text": {"body": text}, "timestamp": "1"}]}}]}]}


async def _first_free(db_session, tenant):
    settings = await booking.load_settings(db_session, tenant.id)
    zone = tenant_zone(tenant)
    [slot] = await booking.free_slots(db_session, tenant, settings, zone, datetime.now(timezone.utc), limit=1)
    return booking.slot_id(slot), booking.slots_for_ai([slot], zone)[0]["label"]


@pytest.mark.asyncio
async def test_booking_through_whatsapp_sends_fixed_confirmation_and_alerts(client, db_session, unique_email, wa, outbox, llm):
    sent, _ = wa
    tenant = await _dealer(db_session, unique_email, phone_number_id="pn-book-1")
    cp = ContactPoint(tenant_id=tenant.id, code="moussa-b1", name="Moussa", greeting="Bonjour",
                      owner_name="Moussa", owner_email="moussa@example.com", channel="COMMERCIAL")
    db_session.add(cp)
    await db_session.commit()
    slot, label = await _first_free(db_session, tenant)
    llm([
        tool_use_response("request_appointment", {"kind": "ESSAI", "slot": slot, "vehicle": "Peugeot 5008"}),
        text_response("Parfait, c'est noté !"),
    ])

    await client.post("/webhooks/whatsapp", json=_payload("pn-book-1", "221700003001", f"Le premier créneau [W:moussa-b1]"))

    assert sent[0].startswith("Parfait, c'est noté !")
    assert sent[1] == f"Bonjour ! Votre essai (Peugeot 5008) est confirmé le {label}. À bientôt chez Auto Plus !"
    assert sorted(m["to"] for m in outbox) == sorted([unique_email, "moussa@example.com"])
    assert all(m["subject"].startswith("Nouveau rendez-vous") and label in m["subject"] for m in outbox)
    assert "Client amené par : Moussa" in outbox[0]["body"]
    stored = (await db_session.execute(select(Message).where(Message.message_type == "appointment_confirmed"))).scalars().all()
    assert [m.content for m in stored] == [sent[1]]
    conversation = (await db_session.execute(select(Conversation).where(Conversation.tenant_id == tenant.id)
                                             .execution_options(populate_existing=True))).scalar_one()
    assert conversation.status == ConversationStatus.ACTIVE


@pytest.mark.asyncio
async def test_confirmation_refused_by_meta_is_not_recorded(client, db_session, unique_email, wa, outbox, llm):
    sent, recorder = wa
    recorder.fail_on = "est confirmé"
    tenant = await _dealer(db_session, unique_email, phone_number_id="pn-book-2")
    slot, _ = await _first_free(db_session, tenant)
    llm([tool_use_response("request_appointment", {"kind": "VISITE", "slot": slot}), text_response("C'est noté !")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-book-2", "221700003002", "Le premier"))

    assert (await db_session.execute(select(Message).where(Message.message_type == "appointment_confirmed"))).scalars().all() == []
    [appointment] = (await db_session.execute(select(AppointmentRequest))).scalars().all()
    assert appointment.status == "CONFIRMED"  # reste visible et confirmé dans la page Rendez-vous
    assert [m["to"] for m in outbox] == [unique_email]


# --- Réglages -----------------------------------------------------------------------------

async def _auth(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_settings_default_then_saved_with_preview(client, db_session, unique_email):
    await _dealer(db_session, unique_email, hours=False)
    headers = await _auth(client, unique_email)

    default = (await client.get("/api/v1/appointments/settings", headers=headers)).json()
    assert (default["online_booking"], default["slot_minutes"], default["capacity"], default["timezone"]) == (False, 60, 1, "Africa/Dakar")

    saved = await client.put("/api/v1/appointments/settings", headers=headers, json={
        "online_booking": True, "opening_hours": ALL_DAY, "slot_minutes": 45, "capacity": 2})
    assert saved.status_code == 200
    body = saved.json()
    assert body["slot_minutes"] == 45 and len(body["preview"]) == 3
    assert (await client.get("/api/v1/appointments/settings", headers=headers)).json()["capacity"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"online_booking": True, "opening_hours": {}},
    {"opening_hours": {"0": [["18:00", "09:00"]]}},
    {"opening_hours": ALL_DAY, "slot_minutes": 50},
    {"opening_hours": ALL_DAY, "capacity": 0},
    {"opening_hours": ALL_DAY, "capacity": 21},
])
async def test_bad_settings_rejected(client, db_session, unique_email, payload):
    await _dealer(db_session, unique_email, hours=False)
    r = await client.put("/api/v1/appointments/settings", headers=await _auth(client, unique_email), json=payload)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_agent_cannot_change_settings(client, db_session, unique_email):
    tenant = await _dealer(db_session, unique_email, hours=False)
    db_session.add(User(tenant_id=tenant.id, email=f"agent-{unique_email}", hashed_password=hash_password("x"), full_name="A", role=Role.AGENT))
    await db_session.commit()
    r = await client.put("/api/v1/appointments/settings", headers=await _auth(client, f"agent-{unique_email}"),
                         json={"online_booking": True, "opening_hours": ALL_DAY})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_settings_and_bookings_are_per_shop(client, db_session, unique_email):
    shop = await _dealer(db_session, unique_email)
    other = await _dealer(db_session, f"autre-{unique_email}", capacity=1)
    # L'autre concession remplit le premier créneau : cela ne change rien pour la première.
    slot, _ = await _first_free(db_session, shop)
    other_exec = ToolExecutor(db_session, other.id, await _conversation(db_session, other), business_type=CAR_DEALERSHIP)
    assert (await other_exec.execute("request_appointment", {"kind": "ESSAI", "slot": slot}))["status"] == "appointment_confirmed"
    assert (await _first_free(db_session, shop))[0] == slot
    assert (await client.get("/api/v1/appointments/settings", headers=await _auth(client, f"autre-{unique_email}"))).json()["online_booking"]


@pytest.mark.asyncio
async def test_appointment_list_shows_bob_confirmations(client, db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, await _conversation(db_session, tenant), business_type=CAR_DEALERSHIP)
    slot = (await executor.execute("get_available_slots", {}))["slots"][0]["slot"]
    await executor.execute("request_appointment", {"kind": "ESSAI", "slot": slot})
    await db_session.commit()

    items = (await client.get("/api/v1/appointments?view=confirmed", headers=await _auth(client, unique_email))).json()["items"]
    assert items[0]["confirmed_by_bob"] is True


def test_dashboard_booking_settings_are_escaped():
    html = open("app/static/dashboard/index.html", encoding="utf-8").read()
    assert "s.preview.map(esc)" in html and 'id="booking-enabled"' in html and "saveBookingSettings" in html
    assert "Confirmé par Bob" in html


# --- Lot 29b : verrou, pas de demande en texte libre quand des créneaux existent -------------

@pytest.mark.asyncio
async def test_free_text_request_is_refused_when_slots_exist(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    conversation = await _conversation(db_session, tenant)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)

    result = await executor.execute("request_appointment", {"kind": "ESSAI", "availability": "samedi matin"})

    assert "créneaux libres" in result["error"] and len(result["slots"]) == 3 and "today" in result
    assert (await db_session.execute(select(AppointmentRequest))).scalars().all() == []
    assert conversation.status == ConversationStatus.ACTIVE  # aucun transfert


@pytest.mark.asyncio
async def test_free_text_request_allowed_when_everything_is_full(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email, hours={"0": [["03:00", "04:00"]]})  # un seul créneau par semaine
    first = ToolExecutor(db_session, tenant.id, await _conversation(db_session, tenant), business_type=CAR_DEALERSHIP)
    offered = (await first.execute("get_available_slots", {}))["slots"]
    for slot in offered:
        await first.execute("request_appointment", {"kind": "ESSAI", "slot": slot["slot"]})
    conversation = await _conversation(db_session, tenant)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)

    result = await executor.execute("request_appointment", {"kind": "ESSAI", "availability": "la semaine prochaine"})

    assert result["status"] == "appointment_requested" and conversation.status == ConversationStatus.WAITING_HUMAN


def test_without_slots_bob_never_mentions_internal_booking_nor_transfers_too_early():
    import asyncio  # noqa: F401 — lecture seule des consignes
    from app.agents import tools as tools_module

    source = open(tools_module.__file__, encoding="utf-8").read()
    assert "ne parle pas de créneaux au client" in source
    assert "Ne dis pas que tu transmets sa demande" in source
    assert "seulement APRÈS cet appel" in DEALERSHIP_RULES
