"""Lot 45 — objections et stratégies propres à la concession automobile."""
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.classifier import (
    DEALERSHIP_CLASSIFIER_EXAMPLES,
    build_classifier_prompt,
    get_message_classifier,
    normalize_classification,
)
from app.agents.dependency import get_llm_client
from app.agents.strategies import (
    DEALERSHIP_OBJECTION_PRIORITY,
    GUARDRAILS,
    NO_KNOWLEDGE_INSTRUCTION,
    OBJECTION_PRIORITY,
    STRATEGIES,
    STRATEGIES_BY_CODE,
    select_strategy,
    strategies_for,
)
from app.agents.taxonomy import (
    DEALERSHIP_OBJECTIONS,
    OBJECTIONS,
    TAXONOMY_VERSION,
    is_known_objection,
    objection_label,
    objections_for,
    taxonomy_version,
)
from app.core.security import hash_password
from app.main import app
from app.models.appointment_request import AppointmentRequest
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.knowledge_entry import KnowledgeCategory, KnowledgeEntry
from app.models.message_signal import MessageSignal
from app.models.strategy_settings import TenantStrategySettings
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.business_type import CAR_DEALERSHIP
from app.services.handoff_rules import HandoffSettingsView, TurnDecision, evaluate
from app.services.signal_service import signals_summary
from app.services.strategy_service import MIN_TERMINATED_FOR_RATE, apply_strategy, strategies_summary
from app.tests.fakes import FakeLLMClient, text_response
from app.tests.test_message_signals import FakeClassifier

NEW = {"FINANCEMENT", "REPRISE", "PAPIERS", "ETAT_VEHICULE"}
DEALER_CODES = {s.code for s in STRATEGIES if s.activity == CAR_DEALERSHIP}


# --- Taxonomie ----------------------------------------------------------------------------------------------

def test_store_taxonomy_is_strictly_unchanged():
    assert list(OBJECTIONS) == ["PRIX_TROP_ELEVE", "FRAIS_LIVRAISON", "CONFIANCE", "QUALITE", "DELAI", "RUPTURE_STOCK", "HESITATION"]
    assert TAXONOMY_VERSION == "v1.2" and taxonomy_version() == "v1.2" and taxonomy_version("ONLINE_STORE") == "v1.2"
    assert objections_for(None) is OBJECTIONS and objections_for("ONLINE_STORE") is OBJECTIONS


def test_dealership_taxonomy():
    assert NEW <= set(DEALERSHIP_OBJECTIONS)
    assert "FRAIS_LIVRAISON" not in DEALERSHIP_OBJECTIONS and "QUALITE" not in DEALERSHIP_OBJECTIONS
    assert objections_for(CAR_DEALERSHIP) is DEALERSHIP_OBJECTIONS
    version = taxonomy_version(CAR_DEALERSHIP)
    assert version != TAXONOMY_VERSION and len(version) <= 8  # colonne String(8)
    assert objection_label("RUPTURE_STOCK", CAR_DEALERSHIP) == "Véhicule plus disponible"
    assert objection_label("RUPTURE_STOCK") == "Rupture de stock"
    assert objection_label("PAPIERS") == "Papiers du véhicule"  # sans activité : connu quand même
    assert is_known_objection("REPRISE") and is_known_objection("FRAIS_LIVRAISON") and not is_known_objection("X")


# --- Classificateur -----------------------------------------------------------------------------------------

def test_store_prompt_does_not_change():
    store = build_classifier_prompt()
    assert build_classifier_prompt("ONLINE_STORE") == store
    assert "boutique en ligne" in store and "FINANCEMENT" not in store and "REPRISE" not in store


def test_dealership_prompt():
    prompt = build_classifier_prompt(CAR_DEALERSHIP)
    assert "concession automobile" in prompt
    assert all(f"- {code} :" in prompt for code in DEALERSHIP_OBJECTIONS)
    assert "FRAIS_LIVRAISON" not in prompt and "- QUALITE :" not in prompt
    assert "Vous reprenez ma Corolla" in prompt


def test_dealership_examples_respect_the_dealership_taxonomy():
    found = set()
    for text, expected in DEALERSHIP_CLASSIFIER_EXAMPLES:
        assert normalize_classification(expected, CAR_DEALERSHIP) == expected, text
        found |= set(expected["objections"])
    assert NEW <= found


def test_objections_are_kept_only_for_their_activity():
    raw = {"intents": ["PAIEMENT"], "objections": ["FINANCEMENT", "FRAIS_LIVRAISON", "QUALITE", "HESITATION"]}
    assert normalize_classification(raw)["objections"] == ["FRAIS_LIVRAISON", "QUALITE", "HESITATION"]
    assert normalize_classification(raw, CAR_DEALERSHIP)["objections"] == ["FINANCEMENT", "HESITATION"]


@pytest.mark.asyncio
async def test_the_dealership_prompt_is_sent_only_for_a_dealership():
    store, dealer = FakeClassifier({"intents": ["AUTRE"]}), FakeClassifier({"intents": ["AUTRE"], "objections": ["REPRISE"]})
    await store.classify("Bonjour")
    result = await dealer.classify("Vous reprenez ma voiture ?", business_type=CAR_DEALERSHIP)
    assert store.prompts == [None]
    assert dealer.prompts == [build_classifier_prompt(CAR_DEALERSHIP)] and result["objections"] == ["REPRISE"]


# --- Bibliothèque -------------------------------------------------------------------------------------------

def test_every_dealership_objection_has_strategies_in_priority_order():
    assert set(DEALERSHIP_OBJECTION_PRIORITY) == set(DEALERSHIP_OBJECTIONS)
    assert all(strategies_for(o, CAR_DEALERSHIP) for o in DEALERSHIP_OBJECTIONS)
    assert strategies_for("FRAIS_LIVRAISON", CAR_DEALERSHIP) == []
    assert {s.code for o in OBJECTION_PRIORITY for s in strategies_for(o)}.isdisjoint(DEALER_CODES)
    assert {s.code for o in DEALERSHIP_OBJECTION_PRIORITY for s in strategies_for(o, CAR_DEALERSHIP)} == DEALER_CODES


def test_the_test_drive_is_offered_for_doubts():
    for objection in ("CONFIANCE", "ETAT_VEHICULE", "HESITATION"):
        assert any("essayer" in s.instruction and "get_available_slots" in s.instruction
                   for s in strategies_for(objection, CAR_DEALERSHIP)), objection


def test_dealership_instructions_never_allow_inventing_money_or_condition():
    for code in ("AUTO_REPRISE_ESTIMATION", "AUTO_REPRISE_ATTENTES"):
        assert "aucune valeur" in STRATEGIES_BY_CODE[code].instruction.lower() or "jamais de valeur" in STRATEGIES_BY_CODE[code].instruction.lower()
    for code in ("AUTO_FIN_SOLUTIONS", "AUTO_FIN_VISITE", "AUTO_PRIX_FINANCEMENT"):
        text = STRATEGIES_BY_CODE[code].instruction
        assert "aucun taux" in text or "aucun chiffre" in text, code
    assert "N'affirme JAMAIS qu'il n'a pas eu d'accident" in STRATEGIES_BY_CODE["AUTO_ETAT_FICHE"].instruction
    assert "Ne demande jamais d'acompte" in STRATEGIES_BY_CODE["AUTO_CONFIANCE_FAITS"].instruction
    assert "kind : ESTIMATION_REPRISE" in STRATEGIES_BY_CODE["AUTO_REPRISE_ESTIMATION"].instruction
    for code in ("AUTO_PAPIERS_FAITS", "AUTO_PAPIERS_VOIR"):
        assert "handoff_to_human" in STRATEGIES_BY_CODE[code].instruction


def test_requirements_on_the_knowledge_base():
    assert STRATEGIES_BY_CODE["AUTO_FIN_SOLUTIONS"].requires == {"PAIEMENT"}
    assert STRATEGIES_BY_CODE["AUTO_PRIX_FINANCEMENT"].requires == {"PAIEMENT"}
    assert STRATEGIES_BY_CODE["AUTO_PAPIERS_FAITS"].requires and STRATEGIES_BY_CODE["AUTO_CONFIANCE_FAITS"].requires
    assert not STRATEGIES_BY_CODE["AUTO_FIN_VISITE"].requires and not STRATEGIES_BY_CODE["AUTO_PAPIERS_VOIR"].requires


# --- Tirage -------------------------------------------------------------------------------------------------

def _draws(objections, business_type, known=frozenset(), disabled=frozenset(), n=200):
    rng = random.Random(7)
    return {select_strategy(objections, set(disabled), rng, knowledge_categories=set(known), business_type=business_type)[0].code
            for _ in range(n)}


def test_each_activity_draws_only_its_own_strategies():
    assert _draws(["HESITATION"], CAR_DEALERSHIP) == {"AUTO_HESITATION_CLARIFIER", "AUTO_HESITATION_RESPECTER", "AUTO_HESITATION_ESSAI"}
    assert _draws(["HESITATION"], None) == {"HESITATION_CLARIFIER", "HESITATION_RESPECTER"}
    assert select_strategy(["FRAIS_LIVRAISON"], set(), business_type=CAR_DEALERSHIP) == (None, None)
    assert select_strategy(["REPRISE"], set()) == (None, None)  # la boutique ne connaît pas la reprise


@pytest.mark.parametrize("objections, expected", [
    (["HESITATION", "PAPIERS"], "PAPIERS"),
    (["PRIX_TROP_ELEVE", "FINANCEMENT"], "FINANCEMENT"),
    (["REPRISE", "ETAT_VEHICULE"], "ETAT_VEHICULE"),
    (["PAPIERS", "CONFIANCE"], "CONFIANCE"),
    (["DELAI", "REPRISE"], "REPRISE"),
])
def test_dealership_priority(objections, expected):
    chosen, _ = select_strategy(objections, set(), random.Random(1), business_type=CAR_DEALERSHIP)
    assert chosen.objection == expected


def test_financing_solutions_need_payment_information():
    assert _draws(["FINANCEMENT"], CAR_DEALERSHIP) == {"AUTO_FIN_VISITE"}
    assert _draws(["FINANCEMENT"], CAR_DEALERSHIP, known={"PAIEMENT"}) == {"AUTO_FIN_SOLUTIONS", "AUTO_FIN_VISITE"}
    assert "AUTO_PRIX_FINANCEMENT" not in _draws(["PRIX_TROP_ELEVE"], CAR_DEALERSHIP)
    blocked = select_strategy(["FINANCEMENT"], {"AUTO_FIN_VISITE"}, knowledge_categories=set(), business_type=CAR_DEALERSHIP)
    assert blocked == (None, "FINANCEMENT")


# --- Avec les règles et la base -----------------------------------------------------------------------------

def test_the_financing_objection_leaves_the_rule_to_its_strategies():
    view = HandoffSettingsView()
    question = evaluate({"intents": ["PAIEMENT"], "objections": []}, view, False, dealership=True)
    objection = evaluate({"intents": ["PAIEMENT"], "objections": ["FINANCEMENT"]}, view, False, dealership=True)
    assert question.rule == "DEALER_FINANCING" and objection.rule is None


async def _tenant(db_session, email, pnid=None, business_type=CAR_DEALERSHIP):
    tenant = Tenant(name="Auto Plus", country="SN", currency="XOF", email=email, plan=TenantPlan.PRO, business_type=business_type)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    if pnid:
        db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=pnid, system_user_token="t"))
    await db_session.commit()
    return tenant


@pytest.mark.asyncio
async def test_blocked_objection_gets_the_dealership_fallback(db_session):
    tenant = await _tenant(db_session, "fb@auto.sn")
    db_session.add(TenantStrategySettings(tenant_id=tenant.id, disabled_strategies=["AUTO_FIN_VISITE"]))
    await db_session.commit()
    turn = TurnDecision()
    assert await apply_strategy(db_session, tenant.id, turn, {"intents": ["PAIEMENT"], "objections": ["FINANCEMENT"]},
                                business_type=CAR_DEALERSHIP) is None
    assert "la concession" in turn.instruction and "la boutique" not in turn.instruction
    assert turn.instruction == NO_KNOWLEDGE_INSTRUCTION.replace("la boutique", "la concession")


@pytest.mark.asyncio
async def test_payment_information_unlocks_the_solutions(db_session):
    tenant = await _tenant(db_session, "pay@auto.sn")
    db_session.add(KnowledgeEntry(tenant_id=tenant.id, category=KnowledgeCategory.PAIEMENT, title="Paiement",
                                  content="Comptant ou financement avec notre banque partenaire.", active=True))
    db_session.add(TenantStrategySettings(tenant_id=tenant.id, disabled_strategies=["AUTO_FIN_VISITE"]))
    await db_session.commit()
    turn = TurnDecision()
    chosen = await apply_strategy(db_session, tenant.id, turn, {"intents": ["PAIEMENT"], "objections": ["FINANCEMENT"]},
                                  business_type=CAR_DEALERSHIP)
    assert chosen.code == "AUTO_FIN_SOLUTIONS" and chosen.instruction in turn.instruction and GUARDRAILS in turn.instruction


# --- Bout en bout (webhook) ---------------------------------------------------------------------------------

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

        async def send_image_message(self, *a, **kw):
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Silent)
    state = {}

    def use(classification, replies):
        state["llm"] = FakeLLMClient(replies)
        state["classifier"] = FakeClassifier(classification)
        app.dependency_overrides[get_llm_client] = lambda: state["llm"]
        app.dependency_overrides[get_message_classifier] = lambda: state["classifier"]
        return state

    yield use
    app.dependency_overrides.pop(get_llm_client, None)
    app.dependency_overrides.pop(get_message_classifier, None)


@pytest.mark.asyncio
async def test_financing_objection_through_the_webhook(client, db_session, wire):
    tenant = await _tenant(db_session, "wh@auto.sn", "pn-auto-45")
    state = wire({"intents": ["PAIEMENT"], "objections": ["FINANCEMENT"]},
                 [text_response("Le financement s'étudie avec un conseiller lors d'une visite. Quand pourriez-vous venir ?")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-auto-45", "221700004501", "Vous faites le crédit ?"))

    signal = (await db_session.execute(select(MessageSignal).where(MessageSignal.tenant_id == tenant.id)
                                       .execution_options(populate_existing=True))).scalar_one()
    assert signal.taxonomy_version == taxonomy_version(CAR_DEALERSHIP) and signal.objections == ["FINANCEMENT"]
    assert signal.strategy == "AUTO_FIN_VISITE" and signal.applied_rule is None
    assert STRATEGIES_BY_CODE["AUTO_FIN_VISITE"].instruction in state["llm"].received_systems[0]
    assert state["classifier"].prompts == [build_classifier_prompt(CAR_DEALERSHIP)]


@pytest.mark.asyncio
async def test_store_webhook_is_unchanged(client, db_session, wire):
    tenant = await _tenant(db_session, "shop@auto.sn", "pn-shop-45", business_type="ONLINE_STORE")
    state = wire({"intents": ["AUTRE"], "objections": ["FINANCEMENT", "HESITATION"]}, [text_response("Plutôt le prix ?")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-shop-45", "221700004502", "Je vais réfléchir"))

    signal = (await db_session.execute(select(MessageSignal).where(MessageSignal.tenant_id == tenant.id))).scalar_one()
    assert signal.taxonomy_version == "v1.2" and signal.objections == ["HESITATION"]
    assert signal.strategy in {"HESITATION_CLARIFIER", "HESITATION_RESPECTER"}
    assert state["classifier"].prompts == [None]


# --- Réglages ---------------------------------------------------------------------------------------------

async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_each_activity_sees_its_own_library(client, db_session):
    await _tenant(db_session, "lib@auto.sn")
    await _tenant(db_session, "lib@shop.sn", business_type="ONLINE_STORE")
    dealer = (await client.get("/api/v1/tenants/me/strategy-settings", headers=await _headers(client, "lib@auto.sn"))).json()["objections"]
    shop = (await client.get("/api/v1/tenants/me/strategy-settings", headers=await _headers(client, "lib@shop.sn"))).json()["objections"]

    assert [g["objection"] for g in dealer] == DEALERSHIP_OBJECTION_PRIORITY
    assert {s["code"] for g in dealer for s in g["strategies"]} == DEALER_CODES
    assert next(g for g in dealer if g["objection"] == "RUPTURE_STOCK")["label"] == "Véhicule plus disponible"
    assert [g["objection"] for g in shop] == OBJECTION_PRIORITY
    assert not {s["code"] for g in shop for s in g["strategies"]} & DEALER_CODES
    fin = next(s for g in dealer for s in g["strategies"] if s["code"] == "AUTO_FIN_SOLUTIONS")
    assert fin["available"] is False and fin["requires"] == ["PAIEMENT"]


@pytest.mark.asyncio
async def test_dealership_settings_are_checked_against_its_library(client, db_session):
    await _tenant(db_session, "set@auto.sn")
    headers = await _headers(client, "set@auto.sn")
    refused = await client.put("/api/v1/tenants/me/strategy-settings",
                               json={"disabled": ["AUTO_REPRISE_ESTIMATION", "AUTO_REPRISE_ATTENTES"]}, headers=headers)
    assert refused.status_code == 422 and "Reprise" in refused.json()["detail"]
    ok = await client.put("/api/v1/tenants/me/strategy-settings", json={"disabled": ["AUTO_HESITATION_ESSAI"]}, headers=headers)
    hesitation = next(g for g in ok.json()["objections"] if g["objection"] == "HESITATION")
    assert {s["code"]: s["enabled"] for s in hesitation["strategies"]}["AUTO_HESITATION_ESSAI"] is False
    shop_rule = await client.put("/api/v1/tenants/me/strategy-settings", json={"disabled": ["QUALITE_DETAILS"]}, headers=headers)
    assert shop_rule.status_code == 200  # la règle « au moins une » de la boutique ne concerne pas la concession


# --- Mesure : rendez-vous obtenus -------------------------------------------------------------------------

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


async def _uses(db_session, tenant, code, n, booked, booked_before=0):
    """n conversations où la stratégie a servi ; `booked` avec un rendez-vous après, `booked_before` avant seulement."""
    for i in range(n):
        customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}")
        db_session.add(customer)
        await db_session.flush()
        conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
        db_session.add(conversation)
        await db_session.flush()
        used = NOW - timedelta(days=3)
        for minutes in (0, 10):  # deux utilisations dans la même conversation
            message = Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                              message_type="text", content="…", created_at=used + timedelta(minutes=minutes))
            db_session.add(message)
            await db_session.flush()
            db_session.add(MessageSignal(tenant_id=tenant.id, conversation_id=conversation.id, message_id=message.id,
                                         intents=["AUTRE"], objections=[STRATEGIES_BY_CODE[code].objection], model="f",
                                         taxonomy_version="v1.3auto", message_created_at=message.created_at, strategy=code))
        if i < booked or i < booked + booked_before:
            when = used + (timedelta(hours=1) if i < booked else -timedelta(days=1))
            db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                                              kind="ESSAI", availability="samedi", status="REQUESTED", created_at=when))
    await db_session.commit()


@pytest.mark.asyncio
async def test_dealership_strategies_are_measured_in_appointments(db_session):
    tenant = await _tenant(db_session, "m1@auto.sn")
    await _uses(db_session, tenant, "AUTO_ETAT_ESSAI", 4, booked=1, booked_before=2)

    summary = await strategies_summary(db_session, tenant.id, now=NOW, business_type=CAR_DEALERSHIP)

    assert summary["measure"] == "APPOINTMENTS"
    [row] = summary["strategies"]
    assert row["uses"] == 8 and row["conversations"] == 4 and row["appointments"] == 1  # un rendez-vous d'avant ne compte pas
    assert row["enough_data"] is False and row["appointment_rate_pct"] is None
    assert row["objection_label"] == "État du véhicule" and row["label"] == "Venir inspecter / essayer"


@pytest.mark.asyncio
async def test_dealership_rate_from_the_threshold_and_isolation(db_session):
    tenant = await _tenant(db_session, "m2@auto.sn")
    other = await _tenant(db_session, "m3@auto.sn")
    await _uses(db_session, tenant, "AUTO_CONFIANCE_VOIR", MIN_TERMINATED_FOR_RATE, booked=5)
    await _uses(db_session, other, "AUTO_CONFIANCE_VOIR", 2, booked=2)

    [row] = (await strategies_summary(db_session, tenant.id, now=NOW, business_type=CAR_DEALERSHIP))["strategies"]

    assert row["conversations"] == MIN_TERMINATED_FOR_RATE and row["appointments"] == 5
    assert row["enough_data"] is True and row["appointment_rate_pct"] == 25.0
    old = await strategies_summary(db_session, tenant.id, days=1, now=NOW, business_type=CAR_DEALERSHIP)
    assert old["strategies"] == []
    assert (await strategies_summary(db_session, tenant.id, now=NOW))["measure"] == "PAID"


@pytest.mark.asyncio
async def test_appointment_counts_from_the_first_use(db_session):
    tenant = await _tenant(db_session, "m4@auto.sn")
    await _uses(db_session, tenant, "AUTO_HESITATION_ESSAI", 1, booked=0)
    conversation = (await db_session.execute(select(Conversation).where(Conversation.tenant_id == tenant.id))).scalar_one()
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=conversation.customer_id,
                                      kind="ESSAI", availability="samedi", status="REQUESTED",
                                      created_at=NOW - timedelta(days=3) + timedelta(minutes=5)))  # entre les deux utilisations
    await db_session.commit()

    [row] = (await strategies_summary(db_session, tenant.id, now=NOW, business_type=CAR_DEALERSHIP))["strategies"]

    assert row["appointments"] == 1


@pytest.mark.asyncio
async def test_mistral_receives_the_dealership_prompt():
    from app.agents.classifier import MistralMessageClassifier

    sent = []

    class _Chat:
        async def complete_async(self, **kwargs):
            sent.append(kwargs["messages"][0]["content"])

            class _R:
                choices = [type("C", (), {"message": type("M", (), {"content": '{"intents": ["AUTRE"], "objections": ["PAPIERS"]}'})()})()]
            return _R()

    classifier = MistralMessageClassifier(api_key="k", model="m")
    classifier._client = type("Client", (), {"chat": _Chat()})()
    dealer = await classifier.classify("Elle est dédouanée ?", business_type=CAR_DEALERSHIP)
    store = await classifier.classify("C'est fiable ?")
    assert sent == [build_classifier_prompt(CAR_DEALERSHIP), build_classifier_prompt()]
    assert dealer["objections"] == ["PAPIERS"] and store["objections"] == []


@pytest.mark.asyncio
async def test_signals_summary_counts_dealership_objections(db_session):
    tenant = await _tenant(db_session, "sig@auto.sn")
    await _uses(db_session, tenant, "AUTO_RUPTURE_SIMILAIRE", 1, booked=0)
    await _uses(db_session, tenant, "AUTO_REPRISE_ESTIMATION", 1, booked=0)

    summary = await signals_summary(db_session, tenant.id, now=NOW, business_type=CAR_DEALERSHIP)

    assert {o["label"] for o in summary["objections"]} == {"Véhicule plus disponible", "Reprise"}


# --- Tableau de bord ----------------------------------------------------------------------------------------

def test_dashboard_shows_appointments_for_a_dealership():
    html = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
    body = html[html.index("async function loadStrategies() {"):html.index("// Libellés affichés des statuts")]
    assert 's.measure === "APPOINTMENTS"' in body and "Rendez-vous obtenus" in body and "r.appointment_rate_pct" in body
    labels = html[html.index("const KNOWLEDGE_LABELS"):html.index("async function loadStrategySettings")]
    assert all(f"{c}:" in labels for c in ("ADRESSE", "HORAIRES", "FAQ", "AUTRE"))


@pytest.mark.asyncio
async def test_analytics_api_uses_the_dealership_vocabulary(client, db_session):
    tenant = await _tenant(db_session, "api@auto.sn")
    await _uses(db_session, tenant, "AUTO_RUPTURE_SIMILAIRE", 1, booked=1)
    headers = await _headers(client, "api@auto.sn")

    strategies = (await client.get("/api/v1/analytics/strategies?days=365", headers=headers)).json()
    signals = (await client.get("/api/v1/analytics/signals?days=365", headers=headers)).json()

    assert strategies["measure"] == "APPOINTMENTS" and strategies["strategies"][0]["appointments"] == 1
    assert [o["label"] for o in signals["objections"]] == ["Véhicule plus disponible"]
