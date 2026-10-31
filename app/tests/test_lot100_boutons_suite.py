"""
Lot 100 — boutons sur les autres messages de Bob : rappel d'échéance de contrat, alerte « nouveau bien »,
après le rendez-vous, confirmation de rendez-vous réservé par Bob, reçu après « Vendu ». Aucun appel d'IA.
"""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.appointment_request import AppointmentRequest
from app.models.conversation import Conversation, ConversationStatus, Message
from app.models.customer import Customer
from app.models.product import Product
from app.models.prospect_profile import ProspectProfile
from app.services import followups
from app.services import guided_flow as gf
from app.tests.test_booking import _dealer
from app.tests.test_lot97_parcours_agence import _agence
from app.tests.test_lot98_parcours_courtier import _cabinet


@pytest.fixture(autouse=True)
def guided(monkeypatch):
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)


async def _client(db, tenant):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2250{uuid.uuid4().int % 10**9:09d}", first_name="Awa",
                        marketing_consent=True)
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.flush()
    return customer, conversation


async def _tap(db, tenant, customer, conversation, reply_id):
    return await gf.handle(db, tenant, None, reply_id, customer=customer, conversation=conversation)


def _titles(buttons):
    return [t for _, t in buttons]


def test_titles_fit_whatsapp_and_nothing_without_the_guided_flow(monkeypatch):
    shop = type("T", (), {"business_type": "ONLINE_STORE", "guided_flow_enabled": True})()
    row = type("R", (), {"id": uuid.uuid4()})()
    for buttons in (followups.contract_buttons(shop, row), followups.property_buttons(shop, row),
                    followups.after_visit_buttons(shop, row), followups.booking_buttons(shop, row),
                    followups.receipt_buttons(shop)):
        assert all(len(t) <= 20 for t in _titles(buttons))
    assert _titles(followups.receipt_buttons(shop)) == ["Voir le catalogue", "Suivre ma commande"]
    assert _titles(followups.booking_buttons(shop, row)) == ["Déplacer", "Annuler"]
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", False)
    assert followups.receipt_buttons(shop) is None and followups.contract_buttons(shop, row) is None


# --- Rappel d'échéance de contrat -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_contract_reminder_buttons_lead_to_the_lot_98_answers(db_session, unique_email):
    from datetime import date

    from app.models.insurance_contract import InsuranceContract

    tenant = await _cabinet(db_session, unique_email)
    customer, conversation = await _client(db_session, tenant)
    contract = InsuranceContract(tenant_id=tenant.id, customer_id=customer.id, branch="AUTO", policy_number="777",
                                 expires_on=date(2026, 12, 1), status="ACTIVE")
    db_session.add(contract)
    await db_session.flush()
    buttons = followups.contract_buttons(tenant, contract)
    assert _titles(buttons) == ["Le renouveler", "Le modifier", "Un conseiller"]
    turn = await _tap(db_session, tenant, customer, conversation, buttons[0][0])
    assert turn.handoff and turn.handoff_reason.startswith("Le client souhaite renouveler son contrat Assurance automobile n° 777")


@pytest.mark.asyncio
async def test_renewal_worker_sends_the_buttons(db_session, unique_email, monkeypatch):
    from datetime import date, datetime, timezone

    from app.models.insurance_contract import InsuranceContract
    from app.services import insurance_contracts

    tenant = await _cabinet(db_session, unique_email)
    customer, conversation = await _client(db_session, tenant)
    contract = InsuranceContract(tenant_id=tenant.id, customer_id=customer.id, branch="AUTO", expires_on=date(2026, 12, 1),
                                 term="ANNUEL", status="ACTIVE")
    db_session.add(contract)
    await db_session.commit()
    seen = []

    async def fake(db, tenant, conversation, customer, text, buttons=None):
        seen.append(buttons)
        return True

    async def window_open(*a, **k):
        return True

    monkeypatch.setattr("app.services.human_reply.customer_window_open", window_open)
    monkeypatch.setattr(insurance_contracts, "reminder_due", lambda c, today: True)
    now = datetime(2026, 11, 2, 10, tzinfo=timezone.utc)
    await insurance_contracts.run_for_tenant(db_session, tenant, now, send_email=lambda **m: True, send_whatsapp=fake)
    assert _titles(seen[0]) == ["Le renouveler", "Le modifier", "Un conseiller"]


# --- Alerte « nouveau bien » ----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_not_interested_stops_property_alerts_until_the_project_changes(db_session, unique_email):
    from app.services import property_alerts, real_estate

    tenant, items = await _agence(db_session, unique_email)
    tenant.plan = "PRO"
    customer, conversation = await _client(db_session, tenant)
    customer.email = "awa@exemple.ci"  # un canal possible : seule la volonté du client bloque
    await real_estate.update_project(db_session, tenant.id, customer.id, {"project": "LOCATION", "zone": "Cocody",
                                                                          "budget": "400000"})
    await db_session.commit()
    product = items[0]
    assert _titles(followups.property_buttons(tenant, product)) == ["Voir le bien", "Demander une visite", "Pas intéressé"]
    before = [i for i in await property_alerts.candidates(db_session, tenant, product) if i["customer_id"] == str(customer.id)]
    assert before and before[0]["blocked"] is None
    [ok] = (await _tap(db_session, tenant, customer, conversation, f"g:pi:{product.id}")).actions
    assert ok["body"].startswith("C'est noté : je ne vous enverrai plus de nouveaux biens.")
    [after] = [i for i in await property_alerts.candidates(db_session, tenant, product) if i["customer_id"] == str(customer.id)]
    assert after["blocked"] == "ne veut plus d'alertes (« Pas intéressé »)"
    await real_estate.update_project(db_session, tenant.id, customer.id, {"project": "LOCATION", "zone": "Cocody Angré"})
    [again] = [i for i in await property_alerts.candidates(db_session, tenant, product) if i["customer_id"] == str(customer.id)]
    assert again["blocked"] is None  # il reparle de son projet : les alertes reprennent
    [visit] = (await _tap(db_session, tenant, customer, conversation, f"g:vi:{product.id}")).actions
    assert visit["body"].startswith("Visite : *Appartement 3 pièces, Riviera 3*.")


@pytest.mark.asyncio
async def test_property_alert_is_sent_with_its_buttons(db_session, unique_email, monkeypatch):
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services import property_alerts

    tenant, items = await _agence(db_session, unique_email, pnid="pn-l100-1")
    customer, conversation = await _client(db_session, tenant)
    sent = []

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, to, body):
            sent.append(("text", body))

        async def send_buttons(self, to, body, buttons, image_link=None):
            sent.append(("buttons", body, buttons, image_link))

    async def window_open(*a, **k):
        return True

    monkeypatch.setattr("app.integrations.whatsapp.client.WhatsAppClient", Client)
    monkeypatch.setattr("app.services.human_reply.customer_window_open", window_open)
    item = {"customer_obj": customer, "conversation": conversation, "reason": property_alerts.REASON_NEW}
    assert await property_alerts._send_whatsapp(db_session, tenant, item, items[0])
    assert sent[0] == ("text", "Bonjour Awa, un nouveau bien correspond à votre recherche :")
    assert sent[1][0] == "buttons" and _titles(sent[1][2]) == ["Voir le bien", "Demander une visite", "Pas intéressé"]
    assert sent[1][3] == items[0].image_url
    assert WhatsAppAccount


# --- Après le rendez-vous, confirmation ------------------------------------------------------------------------------

async def _visit(db, tenant, customer, conversation, status="CONFIRMED"):
    from datetime import datetime, timezone

    appointment = AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                                     kind="ESSAI", vehicle_label="Toyota RAV4", availability="x", status=status,
                                     scheduled_at=datetime.now(timezone.utc) + timedelta(days=1), confirmed_by="BOB")
    db.add(appointment)
    await db.commit()
    return appointment


@pytest.mark.asyncio
async def test_after_the_visit_interested_alerts_an_advisor_thinking_is_noted(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _client(db_session, tenant)
    appointment = await _visit(db_session, tenant, customer, conversation)
    assert _titles(followups.after_visit_buttons(tenant, appointment)) == ["Ça m'intéresse", "Je réfléchis", "Un conseiller"]
    hot = await _tap(db_session, tenant, customer, conversation, f"g:ai:{appointment.id}")
    assert hot.handoff and hot.handoff_reason.startswith("Après ") and hot.handoff_reason.endswith("le client est intéressé")
    [calm] = (await _tap(db_session, tenant, customer, conversation, f"g:ar:{appointment.id}")).actions
    assert calm["body"].startswith("Prenez votre temps.")
    note = (await db_session.execute(select(Message).where(Message.message_type == "client_action"))).scalar_one()
    assert note.content.endswith("le client réfléchit")


@pytest.mark.asyncio
async def test_after_visit_followup_is_sent_with_buttons(db_session, unique_email, monkeypatch):
    from datetime import datetime, timezone

    from app.services import appointment_outcome

    tenant = await _dealer(db_session, unique_email)
    customer, conversation = await _client(db_session, tenant)
    appointment = await _visit(db_session, tenant, customer, conversation)
    seen = []

    async def fake(db, tenant, conversation, customer, text, buttons=None):
        seen.append(buttons)
        return True

    async def closes(*a, **k):
        return datetime(2099, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr("app.services.human_reply.reply_window_closes_at", closes)
    channel = await appointment_outcome.send_followup(db_session, tenant, appointment, datetime.now(timezone.utc),
                                                      send_email=lambda **m: True, send_whatsapp=fake)
    assert channel == "WHATSAPP" and _titles(seen[0]) == ["Ça m'intéresse", "Je réfléchis", "Un conseiller"]


@pytest.mark.asyncio
async def test_guided_booking_confirmation_carries_move_and_cancel(db_session, unique_email):
    from app.tests.test_lot96_parcours_concession import _concession

    tenant, items = await _concession(db_session, unique_email)
    customer, conversation = await _client(db_session, tenant)
    [slots] = (await _tap(db_session, tenant, customer, conversation, f"g:e:{items[0].id}")).actions
    turn = await _tap(db_session, tenant, customer, conversation, slots["rows"][0][0])
    [appointment] = turn.booked
    confirmation = turn.actions[0]
    assert confirmation["type"] == "buttons" and confirmation["message_type"] == "appointment_confirmed"
    assert confirmation["buttons"] == [(f"g:rm:{appointment.id}", "Déplacer"), (f"g:rn:{appointment.id}", "Annuler")]
    assert Product and ProspectProfile


def test_ai_booking_confirmation_and_receipt_use_the_buttons():
    from pathlib import Path

    hook = Path("app/api/webhooks/whatsapp.py").read_text(encoding="utf-8")
    assert "followups.booking_buttons(tenant, booked_appointment)" in hook
    orders = Path("app/api/orders/routes.py").read_text(encoding="utf-8")
    assert "followups.receipt_buttons(tenant)" in orders
