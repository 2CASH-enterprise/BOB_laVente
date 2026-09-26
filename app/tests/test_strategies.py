import random
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.agents.classifier import get_message_classifier
from app.agents.dependency import get_llm_client
from app.agents.strategies import (
    GUARDRAILS,
    MIN_TERMINATED_FOR_RATE,
    OBJECTION_PRIORITY,
    STRATEGIES,
    STRATEGIES_BY_CODE,
    select_strategy,
    strategies_for,
    strategy_instruction,
)
from app.agents.taxonomy import OBJECTIONS
from app.agents.tool_definitions import TOOL_DEFINITIONS
from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.main import app
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.message_signal import MessageSignal
from app.models.product import Product
from app.models.sales_opportunity import SalesOpportunity
from app.models.strategy_settings import TenantStrategySettings
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.handoff_rules import FORBID_TRANSFER, TRANSFER_NOW, TurnDecision
from app.services.strategy_service import apply_strategy, strategies_summary
from app.tests.fakes import FakeLLMClient, text_response
from app.tests.test_message_signals import FakeClassifier

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


# --- La bibliothèque elle-même ---------------------------------------------------------

def test_every_tool_cited_in_an_instruction_exists():
    """Une consigne qui cite un outil inexistant pourrait troubler Bob (cas de get_product_details, évité)."""
    tools = {tool["name"] for tool in TOOL_DEFINITIONS}
    for strategy in STRATEGIES:
        cited = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", strategy.instruction))
        assert cited <= tools, (strategy.code, cited - tools)


def test_every_instruction_carries_the_honesty_guardrails():
    for strategy in STRATEGIES:
        text = strategy_instruction(strategy)
        assert GUARDRAILS in text
        assert "n'invente jamais" in text and "fausse urgence" in text


def test_library_is_consistent_with_the_taxonomy():
    assert len(STRATEGIES_BY_CODE) == len(STRATEGIES)  # codes uniques
    assert {s.objection for s in STRATEGIES} <= set(OBJECTIONS)
    assert set(OBJECTION_PRIORITY) == set(OBJECTIONS)
    assert all(strategies_for(objection) for objection in OBJECTIONS)


def test_value_and_quality_strategies_never_invite_invention():
    for code in ("PRIX_VALEUR", "QUALITE_DETAILS"):
        instruction = STRATEGIES_BY_CODE[code].instruction
        assert "description" in instruction
        assert "n'invente" in instruction or "sans rien ajouter" in instruction


# --- Sélection -------------------------------------------------------------------------

def test_most_blocking_objection_wins():
    chosen, _ = select_strategy(["HESITATION", "PRIX_TROP_ELEVE"], set(), random.Random(1))
    assert chosen.objection == "PRIX_TROP_ELEVE"
    confidence, _ = select_strategy(["PRIX_TROP_ELEVE", "CONFIANCE"], set(), random.Random(1))
    assert confidence.objection == "CONFIANCE"


def test_random_alternation_uses_every_enabled_strategy():
    rng = random.Random(42)
    drawn = {select_strategy(["PRIX_TROP_ELEVE"], set(), rng)[0].code for _ in range(200)}
    assert drawn == {"PRIX_VALEUR", "PRIX_ALTERNATIVE", "PRIX_BUDGET"}


def test_disabled_strategies_are_never_drawn():
    rng = random.Random(7)
    drawn = {select_strategy(["PRIX_TROP_ELEVE"], {"PRIX_VALEUR", "PRIX_BUDGET"}, rng)[0].code for _ in range(50)}
    assert drawn == {"PRIX_ALTERNATIVE"}


def test_no_strategy_without_objection_or_when_all_disabled():
    assert select_strategy([], set()) == (None, None)
    assert select_strategy(["HESITATION"], {"HESITATION_CLARIFIER", "HESITATION_RESPECTER"}) == (None, None)


# --- Articulation avec les règles du lot 13 --------------------------------------------

async def _tenant(db_session, email="s@strat.sn", pnid=None):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=TenantPlan.INDEPENDANT)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    if pnid:
        db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=pnid, system_user_token="t"))
    await db_session.commit()
    return tenant


@pytest.mark.asyncio
async def test_rules_always_come_first(db_session):
    tenant = await _tenant(db_session)
    signal = {"intents": ["DEMANDE_REMISE"], "objections": ["PRIX_TROP_ELEVE"]}
    ruled = TurnDecision(mode=FORBID_TRANSFER, rule="DISCOUNT_FIXED_PRICES", instruction="Prix fixes.")
    assert await apply_strategy(db_session, tenant.id, ruled, signal) is None
    assert ruled.instruction == "Prix fixes."  # jamais deux consignes contradictoires

    transfer = TurnDecision(mode=TRANSFER_NOW, rule="HUMAN_REQUEST")
    assert await apply_strategy(db_session, tenant.id, transfer, {"intents": ["DEMANDE_HUMAIN"], "objections": ["CONFIANCE"]}) is None


@pytest.mark.asyncio
async def test_strategy_is_added_when_no_rule_applies(db_session):
    tenant = await _tenant(db_session)
    turn = TurnDecision()
    chosen = await apply_strategy(db_session, tenant.id, turn, {"intents": ["AUTRE"], "objections": ["HESITATION"]}, random.Random(3))
    assert chosen.objection == "HESITATION"
    assert chosen.instruction in turn.instruction and GUARDRAILS in turn.instruction


# --- Webhook ----------------------------------------------------------------------------

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
    state = {}

    def use(classification, replies):
        fake = FakeLLMClient(replies)
        state["llm"] = fake
        app.dependency_overrides[get_llm_client] = lambda: fake
        app.dependency_overrides[get_message_classifier] = lambda: FakeClassifier(classification)
        return state

    yield use
    app.dependency_overrides.pop(get_llm_client, None)
    app.dependency_overrides.pop(get_message_classifier, None)


async def _signal(db_session, tenant_id):
    return (await db_session.execute(select(MessageSignal).where(MessageSignal.tenant_id == tenant_id)
                                     .execution_options(populate_existing=True))).scalar_one()


@pytest.mark.asyncio
async def test_hesitation_gets_a_strategy_and_it_is_traced(client, db_session, wire):
    tenant = await _tenant(db_session, "h@strat.sn", "pn-st-1")
    state = wire({"intents": ["SALUTATION"], "objections": ["HESITATION"]}, [text_response("Qu'est-ce qui vous fait hésiter ?")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-st-1", "221700000301", "Je vais réfléchir"))

    signal = await _signal(db_session, tenant.id)
    assert signal.strategy in {"HESITATION_CLARIFIER", "HESITATION_RESPECTER"}
    system = state["llm"].received_systems[0]
    assert "CONSIGNE POUR CE MESSAGE" in system
    assert STRATEGIES_BY_CODE[signal.strategy].instruction in system


@pytest.mark.asyncio
async def test_merchant_choice_is_respected(client, db_session, wire):
    tenant = await _tenant(db_session, "c@strat.sn", "pn-st-2")
    db_session.add(TenantStrategySettings(tenant_id=tenant.id, disabled_strategies=["HESITATION_RESPECTER"]))
    await db_session.commit()
    wire({"intents": ["AUTRE"], "objections": ["HESITATION"]}, [text_response("Plutôt le prix ou le modèle ?")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-st-2", "221700000302", "Laissez-moi voir ce soir"))

    assert (await _signal(db_session, tenant.id)).strategy == "HESITATION_CLARIFIER"


@pytest.mark.asyncio
async def test_discount_rule_leaves_no_room_for_a_price_strategy(client, db_session, wire):
    tenant = await _tenant(db_session, "d@strat.sn", "pn-st-3")
    state = wire({"intents": ["DEMANDE_REMISE"], "objections": ["PRIX_TROP_ELEVE"], "offered_amount": 250000},
                 [text_response("Nos prix sont fixes.")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-st-3", "221700000303", "Trop cher, 250000 ?"))

    signal = await _signal(db_session, tenant.id)
    assert signal.applied_rule == "DISCOUNT_FIXED_PRICES" and signal.strategy is None
    system = state["llm"].received_systems[0]
    assert all(s.instruction not in system for s in strategies_for("PRIX_TROP_ELEVE"))


@pytest.mark.asyncio
async def test_no_strategy_is_recorded_during_an_outage(client, db_session, wire, monkeypatch):
    from app.core.config import get_settings
    from app.tests.test_ai_resilience import ApiError, ScriptedLLM

    monkeypatch.setattr(get_settings(), "llm_retry_delays", [0.0, 0.0])
    tenant = await _tenant(db_session, "o@strat.sn", "pn-st-4")
    wire({"intents": ["AUTRE"], "objections": ["CONFIANCE"]}, [])
    app.dependency_overrides[get_llm_client] = lambda: ScriptedLLM([ApiError(503)])

    await client.post("/webhooks/whatsapp", json=_payload("pn-st-4", "221700000304", "C'est fiable ?"))

    signal = await _signal(db_session, tenant.id)
    assert signal.strategy is None and signal.applied_rule == "AI_OUTAGE_RETRY_LATER"


# --- Description produit transmise à Bob ------------------------------------------------

@pytest.mark.asyncio
async def test_product_description_is_given_truncated_and_cost_price_never(db_session):
    tenant = await _tenant(db_session, "p@strat.sn")
    product = Product(tenant_id=tenant.id, sku="SAC", name="Sac cuir", description="Cuir pleine fleur. " * 40,
                      price=45000, cost_price=20000, currency="XOF", stock_quantity=3)
    db_session.add(product)
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, None, customer_id=uuid.uuid4())

    data = await executor._product_to_dict(product)

    assert data["description"].startswith("Cuir pleine fleur.")
    assert len(data["description"]) <= 301 and data["description"].endswith("…")
    assert "cost_price" not in data and 20000 not in data.values()


@pytest.mark.asyncio
async def test_empty_description_is_none(db_session):
    tenant = await _tenant(db_session, "e@strat.sn")
    product = Product(tenant_id=tenant.id, sku="X", name="X", description="  ", price=1, currency="XOF", stock_quantity=1)
    db_session.add(product)
    await db_session.commit()
    assert (await ToolExecutor(db_session, tenant.id, None, customer_id=uuid.uuid4())._product_to_dict(product))["description"] is None


# --- Réglages (API) ------------------------------------------------------------------------

async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_settings_default_all_enabled_then_disable_one(client, db_session):
    await _tenant(db_session, "api@strat.sn")
    headers = await _headers(client, "api@strat.sn")

    library = (await client.get("/api/v1/tenants/me/strategy-settings", headers=headers)).json()["objections"]
    assert all(s["enabled"] for group in library for s in group["strategies"])
    assert library[0]["objection"] == "CONFIANCE"  # ordre de priorité

    r = await client.put("/api/v1/tenants/me/strategy-settings", json={"disabled": ["PRIX_BUDGET"]}, headers=headers)
    price = next(g for g in r.json()["objections"] if g["objection"] == "PRIX_TROP_ELEVE")
    assert {s["code"]: s["enabled"] for s in price["strategies"]} == {"PRIX_VALEUR": True, "PRIX_ALTERNATIVE": True, "PRIX_BUDGET": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled, message", [
    (["QUALITE_DETAILS"], "au moins une stratégie"),
    (["HESITATION_CLARIFIER", "HESITATION_RESPECTER"], "au moins une stratégie"),
    (["STRATEGIE_INVENTEE"], "inconnues"),
])
async def test_invalid_settings_are_refused(client, db_session, disabled, message):
    await _tenant(db_session, "bad@strat.sn")
    r = await client.put("/api/v1/tenants/me/strategy-settings", json={"disabled": disabled},
                         headers=await _headers(client, "bad@strat.sn"))
    assert r.status_code == 422 and message in r.json()["detail"]


@pytest.mark.asyncio
async def test_settings_require_admin_and_are_isolated(client, db_session):
    await _tenant(db_session, "a@strat.sn")
    tenant_b = await _tenant(db_session, "b@strat.sn")
    db_session.add(User(tenant_id=tenant_b.id, email="agent@strat.sn", hashed_password=hash_password("x"), full_name="A", role=Role.AGENT))
    await db_session.commit()

    await client.put("/api/v1/tenants/me/strategy-settings", json={"disabled": ["PRIX_BUDGET"]}, headers=await _headers(client, "a@strat.sn"))
    forbidden = await client.put("/api/v1/tenants/me/strategy-settings", json={"disabled": []}, headers=await _headers(client, "agent@strat.sn"))
    seen_by_b = (await client.get("/api/v1/tenants/me/strategy-settings", headers=await _headers(client, "b@strat.sn"))).json()

    assert forbidden.status_code == 403
    assert all(s["enabled"] for group in seen_by_b["objections"] for s in group["strategies"])


# --- Mesure et seuil ---------------------------------------------------------------------

async def _opportunities_with_strategy(db_session, tenant, code, terminated, paid, in_progress=0):
    for i in range(terminated + in_progress):
        customer = Customer(tenant_id=tenant.id, whatsapp_number=f"22170{code[:3]}{i:04d}")
        db_session.add(customer)
        await db_session.flush()
        conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
        db_session.add(conversation)
        await db_session.flush()
        started = NOW - timedelta(days=10)
        message = Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                          message_type="text", content="…", created_at=started + timedelta(minutes=5))
        db_session.add(message)
        await db_session.flush()
        db_session.add(MessageSignal(tenant_id=tenant.id, conversation_id=conversation.id, message_id=message.id,
                                     intents=["AUTRE"], objections=[STRATEGIES_BY_CODE[code].objection], model="f",
                                     taxonomy_version="v1.1", message_created_at=message.created_at, strategy=code))
        outcome = "IN_PROGRESS" if i >= terminated else ("PAID" if i < paid else "ABANDONED")
        db_session.add(SalesOpportunity(
            id=uuid.uuid4(), tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
            started_at=started, last_customer_message_at=started, last_activity_at=started, outcome=outcome,
            is_first_opportunity=True, is_returning_buyer=False, followup_sent=False, customer_message_count=1,
            message_count=1, order_count=0, order_amount=0, paid_amount=0, computed_at=NOW,
        ))
    await db_session.commit()


@pytest.mark.asyncio
async def test_no_rate_below_the_threshold(db_session):
    tenant = await _tenant(db_session, "t1@strat.sn")
    await _opportunities_with_strategy(db_session, tenant, "HESITATION_CLARIFIER", terminated=MIN_TERMINATED_FOR_RATE - 1, paid=12, in_progress=3)

    [row] = (await strategies_summary(db_session, tenant.id, now=NOW))["strategies"]

    assert row["terminated"] == MIN_TERMINATED_FOR_RATE - 1 and row["paid"] == 12
    assert row["enough_data"] is False and row["conversion_rate_pct"] is None
    assert row["uses"] == MIN_TERMINATED_FOR_RATE - 1 + 3


@pytest.mark.asyncio
async def test_rate_from_the_threshold(db_session):
    tenant = await _tenant(db_session, "t2@strat.sn")
    await _opportunities_with_strategy(db_session, tenant, "PRIX_BUDGET", terminated=MIN_TERMINATED_FOR_RATE, paid=5)

    [row] = (await strategies_summary(db_session, tenant.id, now=NOW))["strategies"]

    assert row["enough_data"] is True and row["conversion_rate_pct"] == 25.0
    assert row["objection_label"] == "Prix trop élevé" and row["label"] == "Budget"


@pytest.mark.asyncio
async def test_strategies_api_and_conversation_label(client, db_session, wire):
    tenant = await _tenant(db_session, "l@strat.sn", "pn-st-5")
    wire({"intents": ["AUTRE"], "objections": ["QUALITE"]}, [text_response("Voici ses caractéristiques.")])
    await client.post("/webhooks/whatsapp", json=_payload("pn-st-5", "221700000305", "C'est de la bonne qualité ?"))
    headers = await _headers(client, "l@strat.sn")
    conversation = (await db_session.execute(select(Conversation).where(Conversation.tenant_id == tenant.id))).scalar_one()

    detail = (await client.get(f"/api/v1/conversations/{conversation.id}", headers=headers)).json()
    summary = (await client.get("/api/v1/analytics/strategies", headers=headers)).json()

    customer_message = next(m for m in detail["messages"] if m["sender"] == "CUSTOMER")
    assert customer_message["signals"]["strategy"] == "Détails"
    assert summary["min_terminated_for_rate"] == MIN_TERMINATED_FOR_RATE
    assert [r["code"] for r in summary["strategies"]] == ["QUALITE_DETAILS"]


# --- Correctif : jamais de condition inventée (incident réel du 26/09) -------------------

from app.agents.prompts import _format_missing_conditions  # noqa: E402
from app.agents.strategies import NO_KNOWLEDGE_INSTRUCTION  # noqa: E402
from app.models.knowledge_entry import KnowledgeCategory, KnowledgeEntry  # noqa: E402


def test_incident_empty_knowledge_never_draws_reassure_by_facts():
    """26/09 : base vide, « Rassurer par les faits » tirée → « retours sous 14 jours » inventés."""
    rng = random.Random(0)
    drawn = {select_strategy(["CONFIANCE"], set(), rng, knowledge_categories=set())[0].code for _ in range(100)}
    assert drawn == {"CONFIANCE_COMPRENDRE"}


def test_reassure_by_facts_becomes_available_with_conditions():
    rng = random.Random(0)
    drawn = {select_strategy(["CONFIANCE"], set(), rng, knowledge_categories={"PAIEMENT"})[0].code for _ in range(100)}
    assert drawn == {"CONFIANCE_COMPRENDRE", "CONFIANCE_FAITS"}


def test_delivery_objection_without_delivery_info_gets_the_fallback():
    assert select_strategy(["FRAIS_LIVRAISON"], set(), knowledge_categories={"PAIEMENT"}) == (None, "FRAIS_LIVRAISON")
    chosen, blocked = select_strategy(["FRAIS_LIVRAISON"], set(), random.Random(1), knowledge_categories={"LIVRAISON"})
    assert chosen.objection == "FRAIS_LIVRAISON" and blocked is None


def test_merchant_disabling_everything_still_means_free_answer_not_fallback():
    assert select_strategy(["HESITATION"], {"HESITATION_CLARIFIER", "HESITATION_RESPECTER"}, knowledge_categories=set()) == (None, None)


@pytest.mark.asyncio
async def test_fallback_instruction_forbids_any_condition(db_session):
    tenant = await _tenant(db_session, "fb@strat.sn")
    turn = TurnDecision()

    chosen = await apply_strategy(db_session, tenant.id, turn, {"intents": ["LIVRAISON"], "objections": ["FRAIS_LIVRAISON"]})

    assert chosen is None
    assert turn.instruction == NO_KNOWLEDGE_INSTRUCTION
    assert "N'affirme AUCUNE condition" in NO_KNOWLEDGE_INSTRUCTION


def test_prompt_lists_what_the_shop_did_not_provide():
    text = _format_missing_conditions([])
    assert "INFORMATIONS NON RENSEIGNÉES" in text
    for topic in ("le paiement", "la livraison", "les retours et remboursements", "la garantie"):
        assert topic in text


def test_prompt_omits_the_section_when_everything_is_provided():
    entries = [KnowledgeEntry(category=c, title="t", content="c") for c in
               (KnowledgeCategory.PAIEMENT, KnowledgeCategory.LIVRAISON, KnowledgeCategory.RETOUR, KnowledgeCategory.GARANTIE)]
    assert _format_missing_conditions(entries) == ""


def test_prompt_only_lists_missing_topics():
    text = _format_missing_conditions([KnowledgeEntry(category=KnowledgeCategory.LIVRAISON, title="t", content="c")])
    assert "la livraison" not in text and "le paiement" in text


@pytest.mark.asyncio
async def test_incident_replayed_through_the_webhook(client, db_session, wire):
    tenant = await _tenant(db_session, "inc@strat.sn", "pn-st-6")
    state = wire({"intents": ["AUTRE"], "objections": ["CONFIANCE"]}, [text_response("Qu'est-ce qui vous inquiète ?")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-st-6", "221700000306", "Vous êtes sérieux ? J'ai peur de payer pour rien"))

    assert (await _signal(db_session, tenant.id)).strategy == "CONFIANCE_COMPRENDRE"
    system = state["llm"].received_systems[0]
    assert "INFORMATIONS NON RENSEIGNÉES" in system and "les retours et remboursements" in system
    assert STRATEGIES_BY_CODE["CONFIANCE_FAITS"].instruction not in system
    assert "N'affirme JAMAIS une condition commerciale" in system


@pytest.mark.asyncio
async def test_inactive_knowledge_entries_do_not_count(db_session):
    tenant = await _tenant(db_session, "ina@strat.sn")
    db_session.add(KnowledgeEntry(tenant_id=tenant.id, category=KnowledgeCategory.PAIEMENT, title="Paiement",
                                  content="Paiement à la livraison", active=False))
    await db_session.commit()
    turn = TurnDecision()
    chosen = await apply_strategy(db_session, tenant.id, turn, {"intents": ["AUTRE"], "objections": ["CONFIANCE"]}, random.Random(5))
    assert chosen.code == "CONFIANCE_COMPRENDRE"


@pytest.mark.asyncio
async def test_settings_show_why_a_strategy_is_inactive(client, db_session):
    tenant = await _tenant(db_session, "why@strat.sn")
    headers = await _headers(client, "why@strat.sn")

    def state(library):
        return {s["code"]: s["available"] for g in library["objections"] for s in g["strategies"]}

    before = state((await client.get("/api/v1/tenants/me/strategy-settings", headers=headers)).json())
    assert before["CONFIANCE_FAITS"] is False and before["LIVRAISON_EXPLIQUER"] is False
    assert before["CONFIANCE_COMPRENDRE"] is True and before["PRIX_BUDGET"] is True

    db_session.add(KnowledgeEntry(tenant_id=tenant.id, category=KnowledgeCategory.LIVRAISON, title="Livraison",
                                  content="Livraison à Dakar en 48 h, 2 000 XOF", active=True))
    await db_session.commit()
    after = state((await client.get("/api/v1/tenants/me/strategy-settings", headers=headers)).json())
    assert after["CONFIANCE_FAITS"] is True and after["LIVRAISON_EXPLIQUER"] is True
