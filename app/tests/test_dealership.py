"""
Lot 24 — mode concession automobile.

Un seul Bob : en concession, il ne vend pas sur WhatsApp. Il renseigne, qualifie et obtient un
rendez-vous que confirme un conseiller. Les outils de vente lui sont RETIRÉS par le code, et
aucun chiffre de financement ou de reprise ne peut atteindre le client.
"""
import uuid

import pytest
from sqlalchemy import select

from app.agents.prompts import BASE_RULES, DEALERSHIP_RULES, build_system_prompt
from app.agents.tool_definitions import TOOL_DEFINITIONS
from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.main import app
from app.models.appointment_request import AppointmentRequest
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation, ConversationStatus, Message
from app.models.customer import Customer
from app.models.negotiation_settings import TenantNegotiationSettings
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services.business_type import (
    CAR_DEALERSHIP,
    DEALERSHIP_TOOLS,
    ONLINE_STORE,
    SALES_TOOLS,
    tools_for,
)
from app.services.finance_guard import FINANCE_MESSAGE, FINANCE_MESSAGE_NO_TRANSFER, contains_financing_figure
from app.services.handoff_rules import (
    ALLOW_TRANSFER,
    FORBID_TRANSFER,
    HandoffSettingsView,
    TurnDecision,
    decide_turn,
    evaluate,
)
from app.services.handoff_service import build_handoff_alert
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response
from app.tests.test_handoff_rules import _conversation, _payload, _setup, sig, wire  # noqa: F401

TOOL_NAMES = {t["name"] for t in TOOL_DEFINITIONS}


# --- Outils : retirés à l'IA ET refusés à l'exécution --------------------------------------

def test_online_store_keeps_every_sales_tool_and_never_sees_appointments():
    names = {t["name"] for t in tools_for(ONLINE_STORE, TOOL_DEFINITIONS)}
    assert names == TOOL_NAMES - DEALERSHIP_TOOLS
    assert {t["name"] for t in tools_for(None, TOOL_DEFINITIONS)} == names  # défaut = boutique en ligne


def test_dealership_loses_every_sales_tool_and_gets_appointments():
    names = {t["name"] for t in tools_for(CAR_DEALERSHIP, TOOL_DEFINITIONS)}
    assert names == TOOL_NAMES - SALES_TOOLS
    assert "request_appointment" in names and not names & SALES_TOOLS


async def _shop(db_session, email, business_type=ONLINE_STORE, plan=TenantPlan.PRO):
    tenant = Tenant(name="Auto Plus", country="SN", currency="XOF", email=email, plan=plan, business_type=business_type)
    db_session.add(tenant)
    await db_session.flush()
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    return tenant, conversation


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(SALES_TOOLS))
async def test_dealership_executor_refuses_sales_tools(db_session, unique_email, tool):
    tenant, conversation = await _shop(db_session, unique_email, CAR_DEALERSHIP)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)

    result = await executor.execute(tool, {"items": [], "product_id": str(uuid.uuid4()), "customer_offer": 1})

    assert "non disponible" in result["error"]


@pytest.mark.asyncio
async def test_online_store_executor_refuses_appointments(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=ONLINE_STORE)

    result = await executor.execute("request_appointment", {"kind": "ESSAI", "availability": "samedi"})

    assert "non disponible" in result["error"]
    assert (await db_session.execute(select(AppointmentRequest))).scalars().all() == []


# --- Demande de rendez-vous ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_appointment_is_recorded_and_handed_to_a_human(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email, CAR_DEALERSHIP)
    car = Product(tenant_id=tenant.id, sku="P3008", name="Peugeot 3008 GT 2021", price=24900, currency="XOF", stock_quantity=1)
    db_session.add(car)
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    executor.turn = TurnDecision(mode=FORBID_TRANSFER, rule="POLITENESS_ONLY")  # n'empêche pas un vrai rendez-vous

    result = await executor.execute("request_appointment", {
        "kind": "ESSAI", "availability": "samedi 10 h", "product_id": str(car.id),
        "notes": "Famille, reprise Clio 2017 90 000 km, intéressé par un financement",
    })

    assert result["status"] == "appointment_requested"
    assert "Ne confirme ni date ni heure" in result["instruction"]
    [appointment] = (await db_session.execute(select(AppointmentRequest))).scalars().all()
    assert (appointment.kind, appointment.availability, appointment.product_id) == ("ESSAI", "samedi 10 h", car.id)
    assert appointment.vehicle_label == "Peugeot 3008 GT 2021" and appointment.status == "REQUESTED"
    assert conversation.status == ConversationStatus.WAITING_HUMAN
    [handoff] = (await db_session.execute(select(Message.content).where(Message.message_type == "handoff"))).scalars().all()
    assert handoff.startswith("Transfert vers un humain : Rendez-vous à confirmer — Essai — Peugeot 3008 GT 2021 — disponibilités : samedi 10 h")
    assert "reprise Clio 2017" in handoff


@pytest.mark.asyncio
async def test_appointment_never_points_to_another_shops_vehicle(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email, CAR_DEALERSHIP)
    other, _ = await _shop(db_session, f"other_{unique_email}", CAR_DEALERSHIP)
    foreign = Product(tenant_id=other.id, sku="X", name="Véhicule d'une autre concession", price=1, currency="XOF", stock_quantity=1)
    db_session.add(foreign)
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)

    await executor.execute("request_appointment", {
        "kind": "VISITE", "availability": "mardi soir", "product_id": str(foreign.id), "vehicle": "le SUV gris",
    })

    [appointment] = (await db_session.execute(select(AppointmentRequest))).scalars().all()
    assert appointment.product_id is None and appointment.vehicle_label == "le SUV gris"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_input", [
    {"kind": "RESERVATION", "availability": "samedi"},
    {"kind": "ESSAI", "availability": "   "},
    {"kind": "ESSAI"},
])
async def test_incomplete_appointment_is_refused_and_nothing_changes(db_session, unique_email, tool_input):
    tenant, conversation = await _shop(db_session, unique_email, CAR_DEALERSHIP)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)

    result = await executor.execute("request_appointment", tool_input)

    assert "error" in result
    assert conversation.status == ConversationStatus.ACTIVE
    assert (await db_session.execute(select(AppointmentRequest))).scalars().all() == []


def test_appointment_alert_email_puts_the_request_first():
    customer = Customer(whatsapp_number="221700000001")
    conversation = Conversation(id=uuid.uuid4())
    subject, body = build_handoff_alert(customer, conversation, "Rendez-vous à confirmer — Essai — Peugeot 3008 — disponibilités : samedi")
    assert subject.startswith("Rendez-vous à confirmer")
    assert "Bob ne la confirme jamais lui-même" in body and "disponibilités : samedi" in body
    other_subject, _ = build_handoff_alert(customer, conversation, "Client mécontent")
    assert other_subject.startswith("Un client attend votre réponse")


# --- Consignes ---------------------------------------------------------------------------

def test_dealership_prompt_has_its_own_rules_and_no_sales_tool():
    dealer = build_system_prompt(Tenant(name="Auto Plus", country="SN", currency="XOF", business_type=CAR_DEALERSHIP))
    assert DEALERSHIP_RULES in dealer and BASE_RULES not in dealer
    for tool in SALES_TOOLS:
        assert tool not in dealer
    assert "ne donne JAMAIS de\n   chiffre" in dealer and "request_appointment" in dealer
    store = build_system_prompt(Tenant(name="Boutique", country="SN", currency="XOF"))
    assert BASE_RULES in store and "request_appointment" not in store


class _RecordingLLM(FakeLLMClient):
    def __init__(self, replies):
        super().__init__(replies)
        self.tools_seen = []

    async def create_message(self, *, system, messages, tools, max_tokens=1024):
        self.tools_seen.append({t["name"] for t in tools})
        return await super().create_message(system=system, messages=messages, tools=tools, max_tokens=max_tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("business_type", [ONLINE_STORE, CAR_DEALERSHIP])
async def test_orchestrator_sends_only_the_allowed_tools(db_session, unique_email, business_type):
    from app.agents.orchestrator import generate_ai_reply

    tenant, conversation = await _shop(db_session, unique_email, business_type)
    llm = _RecordingLLM([text_response("Bonjour !")])

    await generate_ai_reply(db=db_session, tenant=tenant, conversation=conversation, history=[],
                            incoming_text="Bonjour", llm_client=llm)

    assert llm.tools_seen == [{t["name"] for t in tools_for(business_type, TOOL_DEFINITIONS)}]


# --- Règles de transmission ----------------------------------------------------------------

def test_discount_in_a_dealership_goes_to_the_appointment_not_to_negotiation():
    decision = evaluate(sig("DEMANDE_REMISE", amount=20000), HandoffSettingsView(), True, dealership=True)
    assert (decision.mode, decision.rule) == (ALLOW_TRANSFER, "DEALER_PRICE")
    assert "request_appointment" in decision.instruction and "negotiate_price" not in decision.instruction
    # Boutique en ligne : inchangé.
    assert evaluate(sig("DEMANDE_REMISE", amount=20000), HandoffSettingsView(), True).rule == "DISCOUNT_NEGOTIATE"


@pytest.mark.asyncio
async def test_saved_negotiation_setting_is_ignored_in_a_dealership(db_session, unique_email):
    tenant, _ = await _shop(db_session, unique_email, CAR_DEALERSHIP)
    db_session.add(TenantNegotiationSettings(tenant_id=tenant.id, enabled=True, max_discount_pct=10, max_rounds=3))
    await db_session.commit()

    decision = await decide_turn(db_session, tenant, sig("DEMANDE_REMISE", amount=20000))

    assert decision.rule == "DEALER_PRICE"


# --- Garde-fou financement et reprise ------------------------------------------------------

FINANCING_FIGURES = [
    "Vous pourriez avoir des mensualités de 320 €.",
    "Environ 350 € par mois.",
    "45 000 FCFA/mois sur 36 mois.",
    "Avec un taux de 4,9 %, c'est très intéressant.",
    "3,9 % d'intérêt seulement.",
    "Il faudrait un apport de 2 000 €.",
    "En LOA sur 36 mois, c'est possible.",
    "On peut reprendre votre Clio autour de 6 000 €.",
    "Votre Clio vaut à la reprise 5 500 euros.",
    "Estimation : 4 000 €.",
    "Rachat de votre véhicule : 3 500 000 FCFA.",
]

ALLOWED = [
    "La Peugeot 3008 GT 2021 est à 24 900 €.",
    "La 3008 est à 24 900 €, et on pourra estimer votre Clio lors de votre visite.",
    "Elle a 48 000 km, diesel, boîte automatique.",
    "Garantie 12 mois incluse.",
    "Votre conseiller vous fera une proposition de financement personnalisée.",
    "Souhaitez-vous venir samedi à 10 h ?",
    "Le prix est de 12 500 000 FCFA.",
    "Votre Clio 2017 avec 90 000 km : on l'estimera sur place.",
    "Nous estimerons votre Clio lors de la visite. La 3008 est à 24 900 €.",  # deux phrases distinctes
]


@pytest.mark.parametrize("text", FINANCING_FIGURES)
def test_financing_and_trade_in_figures_are_detected(text):
    assert contains_financing_figure(text)


@pytest.mark.parametrize("text", ALLOWED)
def test_vehicle_prices_and_facts_are_allowed(text):
    assert not contains_financing_figure(text)


def test_replacement_messages_carry_no_figure():
    assert not contains_financing_figure(FINANCE_MESSAGE) and not contains_financing_figure(FINANCE_MESSAGE_NO_TRANSFER)


async def _set_business_type(db_session, tenant, business_type):
    tenant.business_type = business_type
    await db_session.commit()


@pytest.mark.asyncio
async def test_webhook_replaces_a_financing_figure_and_hands_over(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-dealer-1")
    await _set_business_type(db_session, tenant, CAR_DEALERSHIP)
    state = wire({"intents": ["AUTRE"], "objections": []},
                 [text_response("Bien sûr ! Comptez environ 320 € par mois sur 48 mois.")])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-dealer-1", "221700000301", "Je peux payer en plusieurs fois ?"))

    assert r.json()["ai_reply"] == FINANCE_MESSAGE
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.WAITING_HUMAN
    traces = (await db_session.execute(select(Message.message_type).where(Message.tenant_id == tenant.id))).scalars().all()
    assert "finance_removed" in traces and "handoff" in traces
    assert len(state["outbox"]) == 1  # le conseiller est prévenu


@pytest.mark.asyncio
async def test_webhook_keeps_the_customer_with_bob_when_a_transfer_is_forbidden(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-dealer-2")
    await _set_business_type(db_session, tenant, CAR_DEALERSHIP)
    wire({"intents": ["SALUTATION"], "objections": []}, [text_response("Merci à vous ! Au fait, 299 €/mois c'est possible.")])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-dealer-2", "221700000302", "Merci"))

    assert r.json()["ai_reply"] == FINANCE_MESSAGE_NO_TRANSFER
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.ACTIVE


@pytest.mark.asyncio
async def test_online_store_replies_are_not_touched(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-dealer-3")
    reply = "Payable en 3 fois : 10 000 FCFA par mois."
    wire({"intents": ["AUTRE"], "objections": []}, [text_response(reply)])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-dealer-3", "221700000303", "En plusieurs fois ?"))

    assert r.json()["ai_reply"] == reply


@pytest.mark.asyncio
async def test_webhook_appointment_end_to_end(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-dealer-4")
    await _set_business_type(db_session, tenant, CAR_DEALERSHIP)
    state = wire({"intents": ["AUTRE"], "objections": []}, [
        tool_use_response("request_appointment", {"kind": "ESSAI", "availability": "samedi 10 h", "vehicle": "3008"}),
        text_response("C'est noté ! Un conseiller va vous confirmer le rendez-vous."),
    ])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-dealer-4", "221700000304", "Samedi 10 h pour un essai"))

    assert "confirmer le rendez-vous" in r.json()["ai_reply"]
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.WAITING_HUMAN
    [email] = state["outbox"]
    assert email["subject"].startswith("Rendez-vous à confirmer") and "samedi 10 h" in email["body"]


# --- Choix du type d'activité ---------------------------------------------------------------

async def _owner(db_session, email, role=Role.OWNER, chosen=False):
    from datetime import datetime, timezone

    tenant = Tenant(name="Nouvelle", country="SN", currency="XOF", email=email,
                    business_type_chosen_at=datetime.now(timezone.utc) if chosen else None)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role))
    await db_session.commit()
    return tenant


async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_new_shop_is_asked_its_business_type_then_it_is_saved(client, db_session, unique_email):
    tenant = await _owner(db_session, unique_email)
    headers = await _headers(client, unique_email)

    before = (await client.get("/api/v1/tenants/me/business-type", headers=headers)).json()
    assert before["business_type"] == ONLINE_STORE and before["chosen"] is False
    assert [o["code"] for o in before["options"]] == [ONLINE_STORE, CAR_DEALERSHIP]

    after = await client.put("/api/v1/tenants/me/business-type", json={"business_type": CAR_DEALERSHIP}, headers=headers)

    assert after.status_code == 200 and after.json()["chosen"] is True
    await db_session.refresh(tenant)
    assert tenant.business_type == CAR_DEALERSHIP
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "BUSINESS_TYPE_CHANGED"))).scalar_one()
    assert audit.details == {"from": ONLINE_STORE, "to": CAR_DEALERSHIP}


@pytest.mark.asyncio
async def test_business_type_needs_admin_and_a_known_value(client, db_session, unique_email):
    await _owner(db_session, unique_email)
    await _owner(db_session, f"agent_{unique_email}", role=Role.AGENT)

    unknown = await client.put("/api/v1/tenants/me/business-type", json={"business_type": "PHARMACY"},
                               headers=await _headers(client, unique_email))
    agent = await client.put("/api/v1/tenants/me/business-type", json={"business_type": CAR_DEALERSHIP},
                             headers=await _headers(client, f"agent_{unique_email}"))

    assert unknown.status_code == 422 and agent.status_code == 403


def test_existing_shops_are_not_asked_again():
    """La migration marque les boutiques existantes comme ayant déjà choisi (pas d'écran surprise)."""
    from pathlib import Path

    migration = (Path(__file__).resolve().parents[2] / "alembic" / "versions" / "a24c0b0e5d11_lot_24_mode_concession.py").read_text()
    assert 'UPDATE tenants SET business_type_chosen_at = now()' in migration
    assert "server_default='ONLINE_STORE'" in migration
