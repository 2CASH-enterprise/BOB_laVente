import uuid

import pytest
from sqlalchemy import select

from app.agents.classifier import get_message_classifier
from app.agents.dependency import get_llm_client
from app.core.security import hash_password
from app.main import app
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.handoff_settings import TenantHandoffSettings
from app.models.message_signal import MessageSignal
from app.models.negotiation_settings import TenantNegotiationSettings
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.handoff_rules import (
    ALLOW_TRANSFER,
    FORBID_TRANSFER,
    TRANSFER_MESSAGE,
    TRANSFER_NOW,
    HandoffSettingsView,
    evaluate,
)
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response
from app.tests.test_message_signals import FakeClassifier

DEFAULTS = HandoffSettingsView()


def sig(*intents, objections=(), amount=None):
    return {"intents": list(intents), "objections": list(objections), "offered_amount": amount}


# --- Le moteur de règles (fonction pure) ------------------------------------------------

def test_no_signal_keeps_previous_behaviour():
    decision = evaluate(None, DEFAULTS, negotiation_active=False)
    assert decision.mode == ALLOW_TRANSFER and decision.rule is None and decision.instruction is None


def test_human_request_always_transfers_now():
    assert evaluate(sig("DEMANDE_HUMAIN"), DEFAULTS, False).mode == TRANSFER_NOW


def test_human_request_has_top_priority():
    decision = evaluate(sig("DEMANDE_REMISE", "RECLAMATION", "DEMANDE_HUMAIN", amount=1000), DEFAULTS, True)
    assert (decision.mode, decision.rule) == (TRANSFER_NOW, "HUMAN_REQUEST")


def test_refund_transfers_now_by_default_and_can_be_disabled():
    assert evaluate(sig("REMBOURSEMENT"), DEFAULTS, False).rule == "REFUND"
    off = evaluate(sig("REMBOURSEMENT"), HandoffSettingsView(refund_transfer=False), False)
    assert off.mode == ALLOW_TRANSFER and off.rule is None


def test_complaint_try_first_by_default():
    decision = evaluate(sig("RECLAMATION"), DEFAULTS, False)
    assert decision.mode == ALLOW_TRANSFER and decision.rule == "COMPLAINT_TRY_FIRST"
    assert "essaie d'abord" in decision.instruction


def test_complaint_can_transfer_now():
    decision = evaluate(sig("RECLAMATION"), HandoffSettingsView(complaint_policy="TRANSFER"), False)
    assert decision.mode == TRANSFER_NOW


def test_discount_with_price_and_negotiation_forces_negotiation():
    decision = evaluate(sig("DEMANDE_REMISE", amount=250000), DEFAULTS, negotiation_active=True, currency="XOF")
    assert decision.mode == FORBID_TRANSFER and decision.rule == "DISCOUNT_NEGOTIATE"
    assert "negotiate_price" in decision.instruction and "250 000 XOF" in decision.instruction


def test_discount_without_price_asks_for_the_price():
    decision = evaluate(sig("DEMANDE_REMISE"), DEFAULTS, negotiation_active=True)
    assert decision.mode == FORBID_TRANSFER and decision.rule == "DISCOUNT_ASK_PRICE"
    assert "quel prix" in decision.instruction


def test_discount_without_negotiation_means_fixed_prices_by_default():
    decision = evaluate(sig("DEMANDE_REMISE", amount=250000), DEFAULTS, negotiation_active=False)
    assert decision.mode == FORBID_TRANSFER and decision.rule == "DISCOUNT_FIXED_PRICES"
    assert "prix sont fixes" in decision.instruction and "N'utilise pas negotiate_price" in decision.instruction


def test_discount_without_negotiation_can_transfer():
    decision = evaluate(sig("DEMANDE_REMISE"), HandoffSettingsView(discount_policy="TRANSFER"), False)
    assert decision.mode == TRANSFER_NOW


def test_politeness_alone_forbids_transfer():
    assert evaluate(sig("SALUTATION"), DEFAULTS, False).mode == FORBID_TRANSFER


def test_politeness_with_hesitation_is_not_politeness_alone():
    assert evaluate(sig("SALUTATION", objections=["HESITATION"]), DEFAULTS, False).mode == ALLOW_TRANSFER


def test_other_alone_never_locks_the_transfer():
    """Un message mal compris (AUTRE) ne doit jamais empêcher un transfert qui serait nécessaire."""
    assert evaluate(sig("AUTRE"), DEFAULTS, False).mode == ALLOW_TRANSFER


# --- Mise en place webhook -------------------------------------------------------------

async def _setup(db_session, email, pnid, plan=TenantPlan.INDEPENDANT, negotiation=None, handoff=None):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=plan)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=pnid, system_user_token="t"))
    if negotiation is not None:
        db_session.add(TenantNegotiationSettings(tenant_id=tenant.id, enabled=negotiation, max_discount_pct=10, max_rounds=3))
    if handoff is not None:
        db_session.add(TenantHandoffSettings(tenant_id=tenant.id, **handoff))
    await db_session.commit()
    return tenant


def _payload(pnid, sender, text):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid}, "messages": [
        {"from": sender, "id": f"wamid.{uuid.uuid4().hex}", "type": "text", "text": {"body": text}, "timestamp": "1"}]}}]}]}


@pytest.fixture
def wire(monkeypatch):
    class _Silent:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, *a, **kw):
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Silent)
    outbox: list[dict] = []
    monkeypatch.setattr("app.api.webhooks.whatsapp.send_email", lambda **kw: outbox.append(kw) or True)
    state = {"outbox": outbox}

    def use(classification, replies):
        fake = FakeLLMClient(replies)
        state["llm"] = fake
        app.dependency_overrides[get_llm_client] = lambda: fake
        app.dependency_overrides[get_message_classifier] = lambda: (FakeClassifier(classification) if classification else None)
        return state

    yield use
    app.dependency_overrides.pop(get_llm_client, None)
    app.dependency_overrides.pop(get_message_classifier, None)


async def _conversation(db_session, tenant_id) -> Conversation:
    return (await db_session.execute(select(Conversation).where(Conversation.tenant_id == tenant_id)
                                     .execution_options(populate_existing=True))).scalar_one()


async def _signal(db_session, tenant_id) -> MessageSignal:
    return (await db_session.execute(select(MessageSignal).where(MessageSignal.tenant_id == tenant_id)
                                     .execution_options(populate_existing=True))).scalar_one()


async def _system_messages(db_session, tenant_id):
    return (await db_session.execute(select(Message.content).where(
        Message.tenant_id == tenant_id, Message.message_type == "handoff"))).scalars().all()


# --- L'incident réel, rejoué -----------------------------------------------------------

@pytest.mark.asyncio
async def test_incident_discount_without_negotiation_gets_fixed_prices_not_a_transfer(client, db_session, unique_email, wire):
    """26/09 : « C'est un peu cher… à 250000 », négociation désactivée → Bob transférait. Plus jamais."""
    tenant = await _setup(db_session, unique_email, "pn-r-1", negotiation=False)
    state = wire(
        {"intents": ["DEMANDE_REMISE"], "objections": ["PRIX_TROP_ELEVE"], "offered_amount": 250000},
        [tool_use_response("handoff_to_human", {"reason": "Négociation"}),  # Bob tente quand même de transférer…
         text_response("Nos prix sont fixes, mais je peux vous proposer un modèle moins cher.")],
    )

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-r-1", "221700000101", "C'est un peu cher, vous pouvez me le faire à 250000"))

    assert r.json()["ai_reply"] == "Nos prix sont fixes, mais je peux vous proposer un modèle moins cher."
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.ACTIVE  # …et le verrou l'en empêche
    assert await _system_messages(db_session, tenant.id) == []
    assert state["outbox"] == []
    assert "prix sont fixes" in state["llm"].received_systems[0]
    signal = await _signal(db_session, tenant.id)
    assert signal.applied_rule == "DISCOUNT_FIXED_PRICES"
    assert signal.handoff_blocked is True
    tool_feedback = repr(state["llm"].received_messages[1])
    assert "Transfert non autorisé" in tool_feedback


# --- Transfert immédiat, sans l'IA ------------------------------------------------------

@pytest.mark.asyncio
async def test_human_request_transfers_now_without_calling_the_ai(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-r-2")
    state = wire({"intents": ["DEMANDE_HUMAIN"], "objections": []}, [])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-r-2", "221700000102", "Je veux parler au responsable"))

    assert r.json()["ai_reply"] == TRANSFER_MESSAGE
    assert state["llm"].call_count == 0
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.WAITING_HUMAN
    assert await _system_messages(db_session, tenant.id) == ["Transfert vers un humain : Règle — demande d'un humain"]
    assert len(state["outbox"]) == 1  # alerte du lot 9
    assert "Raison : Règle — demande d'un humain" in state["outbox"][0]["body"]
    assert (await _signal(db_session, tenant.id)).applied_rule == "HUMAN_REQUEST"


@pytest.mark.asyncio
async def test_refund_setting_off_lets_bob_answer(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-r-3", handoff={"refund_transfer": False})
    state = wire({"intents": ["REMBOURSEMENT"], "objections": []}, [text_response("Je regarde votre commande.")])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-r-3", "221700000103", "Je veux être remboursé"))

    assert r.json()["ai_reply"] == "Je regarde votre commande."
    assert state["llm"].call_count == 1
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.ACTIVE


@pytest.mark.asyncio
async def test_discount_transfer_setting(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-r-4", negotiation=False, handoff={"discount_policy": "TRANSFER"})
    wire({"intents": ["DEMANDE_REMISE"], "objections": []}, [])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-r-4", "221700000104", "Vous faites un prix ?"))

    assert r.json()["ai_reply"] == TRANSFER_MESSAGE
    assert await _system_messages(db_session, tenant.id) == ["Transfert vers un humain : Règle — remise sans négociation : transfert"]


# --- Consignes et verrou quand la négociation est active --------------------------------

@pytest.mark.asyncio
async def test_active_negotiation_with_price_instructs_to_negotiate(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-r-5", negotiation=True)
    state = wire({"intents": ["DEMANDE_REMISE"], "objections": [], "offered_amount": 250000}, [text_response("Pour quel produit ?")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-r-5", "221700000105", "Je vous le prends à 250000"))

    system = state["llm"].received_systems[0]
    assert "CONSIGNE POUR CE MESSAGE" in system and "negotiate_price" in system and "250 000 XOF" in system
    assert (await _signal(db_session, tenant.id)).applied_rule == "DISCOUNT_NEGOTIATE"


@pytest.mark.asyncio
async def test_negotiation_requires_a_paid_plan(client, db_session, unique_email, wire):
    """Mêmes conditions que l'outil : sur le plan gratuit, la négociation n'est pas active."""
    tenant = await _setup(db_session, unique_email, "pn-r-6", plan=TenantPlan.FREE, negotiation=True)
    wire({"intents": ["DEMANDE_REMISE"], "objections": []}, [text_response("Nos prix sont fixes.")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-r-6", "221700000106", "Une petite remise ?"))

    assert (await _signal(db_session, tenant.id)).applied_rule == "DISCOUNT_FIXED_PRICES"


@pytest.mark.asyncio
async def test_politeness_alone_cannot_trigger_a_transfer(client, db_session, unique_email, wire):
    """L'incident du lot 9 : un simple « Bonjour » provoquait un transfert."""
    tenant = await _setup(db_session, unique_email, "pn-r-7")
    wire({"intents": ["SALUTATION"], "objections": []},
         [tool_use_response("handoff_to_human", {"reason": "Ancienne demande"}), text_response("Bonjour !")])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-r-7", "221700000107", "Bonjour"))

    assert r.json()["ai_reply"] == "Bonjour !"
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.ACTIVE


@pytest.mark.asyncio
async def test_allowed_transfer_is_labelled_as_bobs_decision_with_the_rule(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-r-8")
    wire({"intents": ["RECLAMATION"], "objections": []},
         [tool_use_response("handoff_to_human", {"reason": "Colis perdu"}), text_response("Je transmets.")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-r-8", "221700000108", "Mon colis n'est jamais arrivé"))

    [content] = await _system_messages(db_session, tenant.id)
    assert content == "Transfert vers un humain : Colis perdu — décision de Bob (règle : réclamation (Bob tente d'abord))"
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.WAITING_HUMAN


@pytest.mark.asyncio
async def test_without_classification_behaviour_is_unchanged(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-r-9")
    state = wire(None, [tool_use_response("handoff_to_human", {"reason": "Client mécontent"}), text_response("Je transmets.")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-r-9", "221700000109", "Pas content"))

    assert "CONSIGNE POUR CE MESSAGE" not in state["llm"].received_systems[0]
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.WAITING_HUMAN
    assert await _system_messages(db_session, tenant.id) == ["Transfert vers un humain : Client mécontent — décision de Bob"]


@pytest.mark.asyncio
async def test_rules_do_not_wake_bob_while_waiting_for_a_human(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-r-10")
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000110")
    db_session.add(customer)
    await db_session.flush()
    db_session.add(Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.WAITING_HUMAN))
    await db_session.commit()
    state = wire({"intents": ["DEMANDE_HUMAIN"], "objections": []}, [])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-r-10", "221700000110", "Toujours personne ?"))

    assert r.json() == {"status": "received"}
    assert state["llm"].call_count == 0
    assert await _system_messages(db_session, tenant.id) == []


# --- Réglages (API) ----------------------------------------------------------------------

async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_settings_defaults_without_creating_a_row(client, db_session, unique_email):
    tenant = await _setup(db_session, unique_email, "pn-r-11")
    r = await client.get("/api/v1/tenants/me/handoff-settings", headers=await _headers(client, unique_email))
    assert r.json() == {"refund_transfer": True, "complaint_policy": "TRY_FIRST", "discount_policy": "FIXED_PRICES",
                        "ai_outage_policy": "RETRY_LATER", "human_request_transfer": True}
    assert (await db_session.execute(select(TenantHandoffSettings).where(TenantHandoffSettings.tenant_id == tenant.id))).first() is None


@pytest.mark.asyncio
async def test_settings_update_is_audited(client, db_session, unique_email):
    await _setup(db_session, unique_email, "pn-r-12")
    headers = await _headers(client, unique_email)

    r = await client.put("/api/v1/tenants/me/handoff-settings", json={"complaint_policy": "TRANSFER"}, headers=headers)

    assert r.json()["complaint_policy"] == "TRANSFER" and r.json()["refund_transfer"] is True
    assert (await client.get("/api/v1/tenants/me/handoff-settings", headers=headers)).json()["complaint_policy"] == "TRANSFER"
    assert (await db_session.execute(select(AuditLog).where(AuditLog.action == "HANDOFF_SETTINGS_UPDATED"))).first()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"complaint_policy": "JAMAIS"}, {"discount_policy": "NEGOCIER"}])
async def test_invalid_settings_are_rejected(client, db_session, unique_email, payload):
    await _setup(db_session, unique_email, "pn-r-13")
    r = await client.put("/api/v1/tenants/me/handoff-settings", json=payload, headers=await _headers(client, unique_email))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_human_request_cannot_be_disabled(client, db_session, unique_email):
    await _setup(db_session, unique_email, "pn-r-14")
    headers = await _headers(client, unique_email)
    await client.put("/api/v1/tenants/me/handoff-settings", json={"human_request_transfer": False}, headers=headers)
    assert (await client.get("/api/v1/tenants/me/handoff-settings", headers=headers)).json()["human_request_transfer"] is True


@pytest.mark.asyncio
async def test_settings_require_admin_and_are_isolated(client, db_session):
    await _setup(db_session, "a@rules.sn", "pn-r-a")
    tenant_b = await _setup(db_session, "b@rules.sn", "pn-r-b")
    db_session.add(User(tenant_id=tenant_b.id, email="agent@rules.sn", hashed_password=hash_password("x"), full_name="A", role=Role.AGENT))
    await db_session.commit()

    await client.put("/api/v1/tenants/me/handoff-settings", json={"discount_policy": "TRANSFER"}, headers=await _headers(client, "a@rules.sn"))
    forbidden = await client.put("/api/v1/tenants/me/handoff-settings", json={"discount_policy": "TRANSFER"}, headers=await _headers(client, "agent@rules.sn"))

    assert forbidden.status_code == 403
    assert (await client.get("/api/v1/tenants/me/handoff-settings", headers=await _headers(client, "b@rules.sn"))).json()["discount_policy"] == "FIXED_PRICES"
