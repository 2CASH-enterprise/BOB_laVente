"""Lot 44 — cohérence des rendez-vous : calendrier, jour/date, rendez-vous annoncé sans exister, promesses."""
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.orchestrator import generate_ai_reply_detailed
from app.agents.prompts import build_system_prompt
from app.agents.tools import ToolExecutor
from app.models.appointment_request import AppointmentRequest
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.tenant import Tenant, TenantPlan
from app.services import appointment_guard, calendar_check
from app.services.business_type import CAR_DEALERSHIP, ONLINE_STORE
from app.services.promise_guard import contains_human_promise
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response

OCT_2 = date(2026, 10, 2)  # vendredi : jour de l'incident


# --- Calendrier et contrôle jour / date ----------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Je note votre demande pour un rendez-vous samedi 11 octobre à 10h", [("samedi", date(2026, 10, 11), "dimanche")]),
    ("Samedi 11 à 10h", [("samedi", date(2026, 10, 11), "dimanche")]),  # message du conseiller du 02/10
    ("Samedi 3 octobre et samedi 4 octobre", [("samedi", date(2026, 10, 4), "dimanche")]),
    ("SAMEDI 11 OCTOBRE", [("samedi", date(2026, 10, 11), "dimanche")]),
    ("vendredi 1er janvier 2027", []),
    ("vendredi 1er janvier", []),  # déjà passé cette année : c'est le 1er janvier 2027 (un vendredi)
    ("dimanche 10 h", []),  # « dimanche à 10 h », pas « dimanche 10 »
    ("samedi 10 octobre à 10 h", []),
    ("samedi 10 h", []), ("samedi 11h30", []), ("samedi à 10h", []), ("dimanche 11 octobre", []),
    ("lundi 31 novembre", []),  # date impossible : rien à affirmer
    ("Un samedi matin ?", []),
])
def test_mismatches(text, expected):
    found = calendar_check.mismatches(text, OCT_2)
    assert [(f["written"], f["date"], f["real_day"]) for f in found] == expected


def test_clarification_proposes_both_without_choosing():
    [wrong] = calendar_check.mismatches("samedi 11 octobre", OCT_2)
    assert calendar_check.clarification(wrong) == (
        "Petite précision : le 11 octobre est un dimanche. Vous pensiez au samedi 10 octobre ou au dimanche 11 octobre ?")


@pytest.mark.parametrize("day, written, expected", [
    (date(2026, 10, 11), "samedi", date(2026, 10, 10)), (date(2026, 10, 11), "lundi", date(2026, 10, 12)),
    (date(2026, 10, 11), "mercredi", date(2026, 10, 14)), (date(2026, 10, 11), "jeudi", date(2026, 10, 8)),
])
def test_nearest(day, written, expected):
    assert calendar_check.nearest(written, day) == expected


def test_calendar_for_ai():
    text = calendar_check.calendar_for_ai(OCT_2)
    assert text.startswith("Aujourd'hui : vendredi 2 octobre 2026.")
    assert "samedi 10 octobre, dimanche 11 octobre" in text and "vendredi 16 octobre." in text


def test_dealership_prompt_has_the_calendar_and_the_rule():
    tenant = Tenant(name="Auto Plus", country="SN", currency="XOF", business_type=CAR_DEALERSHIP)
    prompt = build_system_prompt(tenant, now=datetime(2026, 10, 2, 12, tzinfo=timezone.utc))
    assert "CALENDRIER" in prompt and "Aujourd'hui : vendredi 2 octobre 2026." in prompt
    assert "donne TOUJOURS le jour ET la date" in prompt
    store = build_system_prompt(Tenant(name="B", country="SN", currency="XOF", business_type=ONLINE_STORE))
    assert "CALENDRIER" not in store


def test_calendar_uses_the_shop_time_zone():
    late = datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc)  # déjà samedi à Paris, encore vendredi à Dakar
    paris = build_system_prompt(Tenant(name="A", country="FR", currency="EUR", business_type=CAR_DEALERSHIP), now=late)
    dakar = build_system_prompt(Tenant(name="A", country="SN", currency="XOF", business_type=CAR_DEALERSHIP), now=late)
    assert "Aujourd'hui : samedi 3 octobre" in paris and "Aujourd'hui : vendredi 2 octobre" in dakar


def test_bob_checks_dates_in_the_shop_time_zone():
    from app.agents.orchestrator import _local_today

    late = datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc)
    early = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)
    paris = Tenant(name="A", country="FR", currency="EUR", business_type=CAR_DEALERSHIP)
    dakar = Tenant(name="A", country="SN", currency="XOF", business_type=CAR_DEALERSHIP)
    montreal = Tenant(name="A", country="CA", currency="CAD", business_type=CAR_DEALERSHIP)
    assert _local_today(paris, late) == date(2026, 10, 3) and _local_today(dakar, late) == date(2026, 10, 2)
    assert _local_today(montreal, early) == date(2026, 10, 2)
    assert _local_today(dakar) == datetime.now(timezone.utc).date()


# --- Rendez-vous annoncé sans exister ---------------------------------------------------------------------

@pytest.mark.parametrize("text, claim", [
    ("Merci, Nouro ! Je note votre demande pour un rendez-vous samedi 11 octobre à 10h à la concession.", True),
    ("Votre demande pour samedi 10 octobre à 10h a bien été enregistrée.", True),
    ("Votre essai est confirmé samedi 10 octobre à 10 h.", True),
    ("C'est noté pour samedi !", True),
    ("Je n'ai pas encore enregistré de rendez-vous.", False),
    ("Aucun créneau n'est disponible samedi 11 octobre.", False),
    ("Quand seriez-vous disponible pour un essai ?", False),
    ("Dès que vous choisissez un créneau, je le réserve.", False),
    ("Je note votre budget de 15 millions.", False),
    ("Votre rendez-vous n'est pas encore confirmé.", False),
    ("Si vous confirmez, c'est noté pour samedi 10 octobre.", False),
])
def test_claims_appointment(text, claim):
    assert appointment_guard.claims_appointment(text) is claim


@pytest.mark.parametrize("text, promise", [
    ("Je transmets votre demande dès que vous me donnez vos disponibilités.", False),  # incident du 02/10
    ("Quand vous serez prêt, je transmets votre demande au conseiller.", False),
    ("Un conseiller vous contactera dès que possible.", True),
    ("Je transmets votre demande à un conseiller, qui vous répondra au plus vite.", True),
    ("Si vous avez d'autres questions, un conseiller vous contactera rapidement.", True),
])
def test_conditional_sentences_are_not_promises(text, promise):
    assert contains_human_promise(text) is promise


# --- Dans la boucle de Bob ------------------------------------------------------------------------------

def _wrong_date_text(today: date) -> tuple[str, date]:
    """Un jour faux pour une date future (calculée par rapport à aujourd'hui, comme en production)."""
    target = today + timedelta(days=9)
    wrong_day = calendar_check.DAYS[(target.weekday() + 6) % 7]
    return f"Je note votre demande pour un rendez-vous {wrong_day} {target.day} {calendar_check.MONTHS[target.month - 1]} à 10h.", target


async def _setup(db, business_type=CAR_DEALERSHIP):
    tenant = Tenant(name="Auto Plus", country="SN", currency="XOF", email=f"t{uuid.uuid4().hex[:6]}@example.com",
                    plan=TenantPlan.PRO, business_type=business_type)
    db.add(tenant)
    await db.flush()
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}")
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.commit()
    return tenant, customer, conversation


async def _reply(db, tenant, conversation, fake, text="Samedi à 10h"):
    return await generate_ai_reply_detailed(db=db, tenant=tenant, conversation=conversation, history=[],
                                            incoming_text=text, llm_client=fake)


@pytest.mark.asyncio
async def test_wrong_date_is_corrected_by_bob(db_session):
    tenant, _, conversation = await _setup(db_session)
    today = datetime.now(timezone.utc).date()
    wrong, target = _wrong_date_text(today)
    question = "Pouvez-vous me préciser le jour souhaité ?"
    fake = FakeLLMClient([text_response(wrong), text_response(question)])
    text, failure = await _reply(db_session, tenant, conversation, fake)
    assert text == question and failure is None
    assert "CONSIGNE (correction) : ta réponse contient un jour qui ne correspond pas à la date" in fake.received_systems[1]


@pytest.mark.asyncio
async def test_wrong_date_twice_becomes_the_fixed_question(db_session):
    tenant, _, conversation = await _setup(db_session)
    wrong, target = _wrong_date_text(datetime.now(timezone.utc).date())
    fake = FakeLLMClient([text_response(wrong), text_response(wrong)])
    text, _ = await _reply(db_session, tenant, conversation, fake)
    assert text.startswith(f"Petite précision : le {target.day} ") and text.endswith("?")


@pytest.mark.asyncio
async def test_announced_appointment_without_tool_is_corrected(db_session):
    tenant, _, conversation = await _setup(db_session)
    claim = "C'est noté pour votre essai !"
    fake = FakeLLMClient([text_response(claim), text_response("Quel jour vous conviendrait ?")])
    text, _ = await _reply(db_session, tenant, conversation, fake)
    assert text == "Quel jour vous conviendrait ?"
    assert "tu as annoncé un rendez-vous" in fake.received_systems[1]
    fake = FakeLLMClient([text_response(claim), text_response(claim)])
    text, _ = await _reply(db_session, tenant, conversation, fake)
    assert text == appointment_guard.FALLBACK


@pytest.mark.asyncio
async def test_announcement_is_fine_after_the_real_booking(db_session):
    tenant, customer, conversation = await _setup(db_session)
    fake = FakeLLMClient([
        tool_use_response("request_appointment", {"kind": "ESSAI", "availability": "samedi matin"}),
        text_response("Votre demande d'essai est bien enregistrée : un conseiller vous confirmera l'heure."),
    ])
    text, _ = await _reply(db_session, tenant, conversation, fake)
    assert text.startswith("Votre demande d'essai est bien enregistrée") and fake.call_count == 2
    assert (await db_session.execute(select(AppointmentRequest))).scalar_one().customer_id == customer.id


@pytest.mark.asyncio
async def test_a_store_is_not_checked(db_session):
    tenant, _, conversation = await _setup(db_session, ONLINE_STORE)
    wrong, _ = _wrong_date_text(datetime.now(timezone.utc).date())
    fake = FakeLLMClient([text_response(wrong)])
    text, _ = await _reply(db_session, tenant, conversation, fake)
    assert text == wrong and fake.call_count == 1


# --- Verrou sur la demande de rendez-vous -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_with_wrong_day_is_refused(db_session):
    tenant, _, conversation = await _setup(db_session)
    today = datetime.now(timezone.utc).date()
    target = today + timedelta(days=9)
    wrong_day = calendar_check.DAYS[(target.weekday() + 6) % 7]
    tools = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    r = await tools.execute("request_appointment", {"kind": "ESSAI",
                                                    "availability": f"{wrong_day} {target.day} {calendar_check.MONTHS[target.month - 1]} à 10h"})
    assert "error" in r and "Petite précision" in r["instruction"]
    assert (await db_session.execute(select(AppointmentRequest))).scalars().all() == []
    assert tools.appointment_checked is False
    right_day = calendar_check.DAYS[target.weekday()]
    r = await tools.execute("request_appointment", {"kind": "ESSAI",
                                                    "availability": f"{right_day} {target.day} {calendar_check.MONTHS[target.month - 1]} à 10h"})
    assert "error" not in r and tools.appointment_checked is True


# --- Tableau de bord ------------------------------------------------------------------------------------------

def test_dashboard_confirmation_summary():
    html = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
    body = html[html.index("async function confirmAppointment(id) {"):html.index("// Lot 36 : issue du rendez-vous.")]
    assert "appointmentChecks(when, a)" in body and "bobConfirm(" in body
    assert "Demande du client" in body and "Confirmer quand même" in body
    checks = html[html.index("function appointmentChecks"):html.index("async function confirmAppointment(id) {")]
    assert "mm % 5 !== 0" in checks and "est inhabituelle" in checks and "vous avez choisi un" in checks
