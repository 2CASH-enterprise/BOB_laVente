"""
Lot 54 — courtier / agent d'assurance : objections et stratégies (mesurées en cotations ou rendez-vous),
score du prospect (échéance et durée du contrat actuel), accueil, démo, statut du cabinet et points du
règlement CIMA 01-24 (identification, réclamations, jamais de crédit, accord du client horodaté).
Commerce et concession strictement inchangés.
"""
import random
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.agents.classifier import INSURANCE_CLASSIFIER_EXAMPLES, build_classifier_prompt, normalize_classification
from app.agents.prompts import build_system_prompt, insurance_identity, insurance_office_info
from app.agents.strategies import (
    GUARDRAILS,
    INSURANCE_GUARDRAILS,
    INSURANCE_NO_KNOWLEDGE_INSTRUCTION,
    INSURANCE_OBJECTION_PRIORITY,
    STRATEGIES,
    no_knowledge_instruction,
    priority_for,
    select_strategy,
    strategies_for,
    strategy_instruction,
)
from app.agents.taxonomy import (
    DEALERSHIP_OBJECTIONS,
    INSURANCE_OBJECTIONS,
    INSURANCE_TAXONOMY_VERSION,
    OBJECTIONS,
    is_known_objection,
    objection_label,
    objections_for,
    taxonomy_version,
)
from app.agents.tool_definitions import TOOL_DEFINITIONS
from app.agents.tools import ToolExecutor
from app.models.appointment_request import AppointmentRequest
from app.models.appointment_settings import TenantAppointmentSettings
from app.models.conversation import Message
from app.models.message_signal import MessageSignal
from app.models.product import Product
from app.models.quote_request import QuoteRequest
from app.models.tenant import Tenant
from app.models.user import Role
from app.services import insurance, insurance_prospect
from app.services.business_type import CAR_DEALERSHIP, INSURANCE_BROKER, ONLINE_STORE, tools_for
from app.services.handoff_rules import ALLOW_TRANSFER, TRANSFER_NOW, HandoffSettingsView, evaluate
from app.tests.fakes import text_response, tool_use_response
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture
from app.tests.test_lot53_courtier import _cabinet, _conversation, _headers, _payload

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
DEMO_HTML = (ROOT / "static" / "instant-demo" / "index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)  # 9 octobre 2026, 10 h à Abidjan (UTC+0)


def _function(name, html=HTML):
    body = html[html.index(f"function {name}("):]
    return body[:body.index("\n}\n")]


# --- Objections du courtier -------------------------------------------------------------------------------

def test_insurance_taxonomy():
    assert set(INSURANCE_OBJECTIONS) == {"PRIX_TROP_ELEVE", "DEJA_ASSURE", "CONFIANCE", "PAS_BESOIN", "COMPARAISON",
                                         "PAIEMENT", "HESITATION"}
    assert objections_for(INSURANCE_BROKER) is INSURANCE_OBJECTIONS
    assert objections_for(CAR_DEALERSHIP) is DEALERSHIP_OBJECTIONS and objections_for(ONLINE_STORE) is OBJECTIONS
    assert taxonomy_version(INSURANCE_BROKER) == INSURANCE_TAXONOMY_VERSION == "v1.4assu" and len(INSURANCE_TAXONOMY_VERSION) <= 8
    assert taxonomy_version(ONLINE_STORE) == "v1.2" and taxonomy_version(CAR_DEALERSHIP) == "v1.3auto"
    assert is_known_objection("DEJA_ASSURE") and is_known_objection("PAS_BESOIN") and not is_known_objection("XYZ")
    assert objection_label("DEJA_ASSURE") == "Déjà assuré"  # sans activité : retrouvé chez le courtier
    assert objection_label("PAIEMENT", INSURANCE_BROKER) == "Paiement de la prime"
    assert objection_label("PRIX_TROP_ELEVE", INSURANCE_BROKER) == "Prix trop élevé"
    assert "PAIEMENT" not in OBJECTIONS and "DEJA_ASSURE" not in DEALERSHIP_OBJECTIONS  # les autres listes sont intactes


def test_insurance_classifier_prompt():
    prompt = build_classifier_prompt(INSURANCE_BROKER)
    assert "cabinet ou une agence d'assurance" in prompt and "- DEJA_ASSURE :" in prompt and "- PAS_BESOIN :" in prompt
    assert "FRAIS_LIVRAISON" not in prompt and "REPRISE" not in prompt and "boutique en ligne" not in prompt
    assert "concession" not in build_classifier_prompt(INSURANCE_BROKER)
    assert "boutique en ligne" in build_classifier_prompt(ONLINE_STORE)  # commerce inchangé
    for _, expected in INSURANCE_CLASSIFIER_EXAMPLES:
        assert set(expected["objections"]) <= set(INSURANCE_OBJECTIONS)
    raw = {"intents": ["AUTRE"], "objections": ["DEJA_ASSURE", "FRAIS_LIVRAISON", "REPRISE"], "offered_amount": 50000}
    assert normalize_classification(raw, INSURANCE_BROKER)["objections"] == ["DEJA_ASSURE"]
    assert normalize_classification(raw, ONLINE_STORE)["objections"] == ["FRAIS_LIVRAISON"]


@pytest.mark.asyncio
async def test_classifier_uses_the_insurance_instructions():
    from app.tests.test_message_signals import FakeClassifier

    classifier = FakeClassifier({"intents": ["AUTRE"], "objections": ["DEJA_ASSURE"], "offered_amount": None})
    result = await classifier.classify("J'ai déjà une assurance", business_type=INSURANCE_BROKER)
    assert result["objections"] == ["DEJA_ASSURE"] and "DEJA_ASSURE" in classifier.prompts[-1]
    await classifier.classify("C'est trop cher")
    assert classifier.prompts[-1] is None  # commerce : consignes par défaut, inchangées


def test_insurance_strategies():
    insurance_strategies = [s for s in STRATEGIES if s.activity == INSURANCE_BROKER]
    assert len(insurance_strategies) == 17 and all(s.code.startswith("ASSU_") for s in insurance_strategies)
    assert priority_for(INSURANCE_BROKER) == INSURANCE_OBJECTION_PRIORITY
    assert set(INSURANCE_OBJECTION_PRIORITY) == set(INSURANCE_OBJECTIONS)
    allowed_tools = {t["name"] for t in tools_for(INSURANCE_BROKER, TOOL_DEFINITIONS)}
    for objection in INSURANCE_OBJECTIONS:
        own = strategies_for(objection, INSURANCE_BROKER)
        assert len(own) >= 2 and all(s.activity == INSURANCE_BROKER for s in own), objection
    for strategy in insurance_strategies:
        text = strategy_instruction(strategy)
        assert INSURANCE_GUARDRAILS in text and GUARDRAILS not in text
        assert not insurance.contains_amount(strategy.instruction), strategy.code  # jamais un chiffre de prix
        for tool in ("update_prospect_profile", "check_stock", "negotiate_price", "create_order", "search_products("):
            assert tool not in strategy.instruction, (strategy.code, tool)
        cited = {t for t in ("update_insurance_request", "get_available_slots", "request_appointment",
                             "handoff_to_human", "search_products") if t in strategy.instruction}
        assert cited <= allowed_tools
    # Règlement CIMA : jamais de crédit ni de paiement différé, jamais de promesse de prise en charge.
    for strategy in strategies_for("PAIEMENT", INSURANCE_BROKER):
        assert "Ne promets JAMAIS de crédit" in strategy.instruction
    for strategy in strategies_for("CONFIANCE", INSURANCE_BROKER)[:2]:
        assert "Ne promets JAMAIS qu'un sinistre sera pris en charge" in strategy.instruction or \
               "Ne promets JAMAIS qu'un sinistre sera pris" in strategy.instruction.replace("\n", " ")
    # Les stratégies de la boutique et de la concession ne servent jamais au courtier, et inversement.
    assert not any(s.activity == INSURANCE_BROKER for o in OBJECTIONS for s in strategies_for(o, ONLINE_STORE))
    assert not any(s.activity == INSURANCE_BROKER for o in DEALERSHIP_OBJECTIONS for s in strategies_for(o, CAR_DEALERSHIP))


def test_insurance_strategy_selection():
    rng = random.Random(3)
    strategy, blocked = select_strategy(["HESITATION", "DEJA_ASSURE"], set(), rng, set(), INSURANCE_BROKER)
    assert blocked is None and strategy.objection == "DEJA_ASSURE"  # priorité : déjà assuré avant l'hésitation
    strategy, _ = select_strategy(["CONFIANCE", "PRIX_TROP_ELEVE"], set(), rng, set(), INSURANCE_BROKER)
    assert strategy.objection == "CONFIANCE" and strategy.code != "ASSU_CONFIANCE_FAITS"  # faits : base vide
    # Paiement : la seule stratégie active exige les moyens de paiement → repli, jamais d'invention.
    strategy, blocked = select_strategy(["PAIEMENT"], {"ASSU_PAIEMENT_CONSEILLER"}, rng, set(), INSURANCE_BROKER)
    assert strategy is None and blocked == "PAIEMENT"
    assert no_knowledge_instruction(INSURANCE_BROKER) == INSURANCE_NO_KNOWLEDGE_INSTRUCTION
    assert "le cabinet n'a pas renseignées" in INSURANCE_NO_KNOWLEDGE_INSTRUCTION
    strategy, _ = select_strategy(["PAIEMENT"], {"ASSU_PAIEMENT_CONSEILLER"}, rng, {"PAIEMENT"}, INSURANCE_BROKER)
    assert strategy.code == "ASSU_PAIEMENT_FAITS"
    # « Rassurer par les faits » exige des informations du cabinet dans la base de connaissances.
    others = {"ASSU_CONFIANCE_CONSEILLER", "ASSU_CONFIANCE_COMPRENDRE"}
    assert select_strategy(["CONFIANCE"], others, rng, set(), INSURANCE_BROKER) == (None, "CONFIANCE")
    assert select_strategy(["CONFIANCE"], others, rng, {"ADRESSE"}, INSURANCE_BROKER)[0].code == "ASSU_CONFIANCE_FAITS"
    # Un code d'une autre activité n'est jamais tiré pour le courtier.
    assert select_strategy(["FRAIS_LIVRAISON", "REPRISE"], set(), rng, set(), INSURANCE_BROKER) == (None, None)


# --- Règles de transfert du courtier (CIMA) ---------------------------------------------------------------

def _rule(signal, insurance_flag=True, known=None):
    return evaluate(signal, HandoffSettingsView(), False, known_categories=known if known is not None else set(),
                    insurance=insurance_flag)


def test_insurance_handoff_rules():
    complaint = _rule({"intents": ["RECLAMATION"], "objections": []})
    assert complaint.mode == ALLOW_TRANSFER and complaint.rule == "INSURANCE_COMPLAINT"
    assert "handoff_to_human" in complaint.instruction and "contact pour les réclamations" in complaint.instruction
    price = _rule({"intents": ["DEMANDE_REMISE"], "objections": ["PRIX_TROP_ELEVE"], "offered_amount": 40000})
    assert price.rule == "INSURANCE_PRICE" and "AUCUN montant" in price.instruction and price.mode == ALLOW_TRANSFER
    payment = _rule({"intents": ["PAIEMENT"], "objections": []})
    assert payment.rule == "INSURANCE_PAYMENT" and "JAMAIS de crédit" in payment.instruction
    # Objection « paiement » reconnue : ce sont ses stratégies (réglables, mesurées) qui s'appliquent.
    assert _rule({"intents": ["PAIEMENT"], "objections": ["PAIEMENT"]}).rule is None
    # Une question de garanties ne part plus au cabinet faute de conditions renseignées : Bob qualifie.
    assert _rule({"intents": ["CONDITIONS_VENTE"], "objections": []}).mode != TRANSFER_NOW
    assert _rule({"intents": ["CONDITIONS_VENTE"], "objections": []}, insurance_flag=False).rule == "MISSING_CONDITIONS"
    # Toujours : demande d'un humain et remboursement.
    assert _rule({"intents": ["DEMANDE_HUMAIN", "RECLAMATION"], "objections": []}).rule == "HUMAN_REQUEST"
    assert _rule({"intents": ["REMBOURSEMENT"], "objections": []}).rule == "REFUND"
    # Commerce inchangé : réclamation = d'abord essayer de résoudre.
    assert _rule({"intents": ["RECLAMATION"], "objections": []}, insurance_flag=False).rule == "COMPLAINT_TRY_FIRST"


@pytest.mark.asyncio
async def test_decide_turn_knows_the_cabinet(db_session):
    from app.services.handoff_rules import decide_turn

    cabinet = await _cabinet(db_session)
    store = await _cabinet(db_session, business_type=ONLINE_STORE)
    signal = {"intents": ["RECLAMATION"], "objections": []}
    assert (await decide_turn(db_session, cabinet, signal)).rule == "INSURANCE_COMPLAINT"
    assert (await decide_turn(db_session, store, signal)).rule == "COMPLAINT_TRY_FIRST"


@pytest.mark.asyncio
async def test_whatsapp_objection_gets_an_insurance_strategy(client, db_session, wire):  # noqa: F811
    tenant = await _cabinet(db_session)
    state = wire({"intents": ["AUTRE"], "objections": ["DEJA_ASSURE"]},
                 [text_response("Je comprends. Quand arrive l'échéance de votre contrat ?")])

    r = await client.post("/webhooks/whatsapp", json=_payload(tenant.pnid, "2250722222222", "J'ai déjà une assurance"))

    assert r.status_code == 200 and r.json()["ai_reply"].startswith("Je comprends")
    signal = (await db_session.execute(select(MessageSignal).where(MessageSignal.tenant_id == tenant.id))).scalar_one()
    assert signal.taxonomy_version == "v1.4assu" and signal.objections == ["DEJA_ASSURE"]
    assert signal.strategy in {"ASSU_DEJA_ECHEANCE", "ASSU_DEJA_BILAN"}
    sent = " ".join(str(x) for x in state["llm"].received_systems + state["llm"].received_messages)
    assert "Le client a déjà une assurance" in sent and "Garde-fous : n'invente jamais de garantie" in sent


# --- Mesure : cotations ou rendez-vous obtenus ------------------------------------------------------------

@pytest.mark.asyncio
async def test_objections_measured_in_quotes_or_appointments(db_session):
    from app.services.signal_service import signals_summary
    from app.services.strategy_service import strategies_summary

    cabinet = await _cabinet(db_session)
    rows = []
    for i in range(3):
        customer, conversation = await _conversation(db_session, cabinet)
        message = Message(tenant_id=cabinet.id, conversation_id=conversation.id, sender="CUSTOMER", message_type="text",
                          content="trop cher", created_at=NOW - timedelta(hours=5))
        db_session.add(message)
        await db_session.flush()
        db_session.add(MessageSignal(tenant_id=cabinet.id, conversation_id=conversation.id, message_id=message.id,
                                     intents=["AUTRE"], objections=["PRIX_TROP_ELEVE"], model="f", taxonomy_version="v1.4assu",
                                     strategy="ASSU_PRIX_ADAPTER", message_created_at=NOW - timedelta(hours=5)))
        rows.append((customer, conversation))
    # 1 : cotation transmise APRÈS l'objection ; 2 : rendez-vous après ; 3 : cotation AVANT (ne compte pas).
    db_session.add(QuoteRequest(tenant_id=cabinet.id, customer_id=rows[0][0].id, conversation_id=rows[0][1].id, branch="AUTO",
                                client_type="PARTICULIER", details={}, status="SUBMITTED", submitted_at=NOW - timedelta(hours=1)))
    db_session.add(AppointmentRequest(tenant_id=cabinet.id, customer_id=rows[1][0].id, conversation_id=rows[1][1].id,
                                      kind="APPEL", availability="x", created_at=NOW - timedelta(hours=2)))
    db_session.add(QuoteRequest(tenant_id=cabinet.id, customer_id=rows[2][0].id, conversation_id=rows[2][1].id, branch="AUTO",
                                client_type="PARTICULIER", details={}, status="SUBMITTED", submitted_at=NOW - timedelta(days=1)))
    # Une cotation d'un autre cabinet ne compte jamais (même rattachée par erreur à cette conversation).
    other = await _cabinet(db_session)
    o_customer, _ = await _conversation(db_session, other)
    db_session.add(QuoteRequest(tenant_id=other.id, customer_id=o_customer.id, conversation_id=rows[2][1].id, branch="AUTO",
                                client_type="PARTICULIER", details={}, status="SUBMITTED", submitted_at=NOW - timedelta(hours=1)))
    await db_session.commit()

    strategies = await strategies_summary(db_session, cabinet.id, now=NOW, business_type=INSURANCE_BROKER)
    assert strategies["measure"] == "APPOINTMENTS" and strategies["outcome_label"] == "Cotations ou rendez-vous obtenus"
    [row] = strategies["strategies"]
    assert (row["code"], row["uses"], row["conversations"], row["appointments"]) == ("ASSU_PRIX_ADAPTER", 3, 3, 2)
    assert row["objection_label"] == "Prix trop élevé"

    signals = await signals_summary(db_session, cabinet.id, now=NOW, business_type=INSURANCE_BROKER)
    assert signals["outcome_label"] == "Cotations ou rendez-vous obtenus"
    [objection] = signals["objections"]
    assert (objection["code"], objection["conversations"], objection["appointments"]) == ("PRIX_TROP_ELEVE", 3, 2)

    # Concession : mêmes chiffres SANS les cotations (inchangée).
    dealer = await strategies_summary(db_session, cabinet.id, now=NOW, business_type=CAR_DEALERSHIP)
    assert dealer["outcome_label"] == "Rendez-vous obtenus" and dealer["strategies"][0]["appointments"] == 1


def test_dashboard_shows_objections_to_the_cabinet():
    assert 'class="card" id="signals-card"' in HTML and 'class="card" id="strategies-card"' in HTML
    assert 'loadHandoffSettings(); loadStrategySettings(); loadFollowupSettings(); }' in HTML
    assert 'const obtained = s.outcome_label || "Rendez-vous obtenus";' in _function("loadStrategies")
    assert "<th>${esc(obtained)}</th>" in _function("loadSignals")
    assert 'return (s.outcome_label || "").startsWith("Cotations");' in _function("insuranceMeasure")
    assert '"Freins à la souscription" : "Freins à l\'achat"' in HTML


# --- Score du prospect ------------------------------------------------------------------------------------

def test_hot_window_follows_the_contract_length():
    assert insurance_prospect.hot_window("MENSUEL") == 15
    assert insurance_prospect.hot_window("TRIMESTRIEL") == 45
    assert insurance_prospect.hot_window("SEMESTRIEL") == 60 and insurance_prospect.hot_window("ANNUEL") == 60
    assert insurance_prospect.hot_window(None) == 60 and insurance_prospect.hot_window("XYZ") == 60


@pytest.mark.parametrize("days_left,term,expected", [
    (15, "MENSUEL", "CHAUD"), (16, "MENSUEL", "TIEDE"), (45, "TRIMESTRIEL", "CHAUD"), (46, "TRIMESTRIEL", "TIEDE"),
    (60, "ANNUEL", "CHAUD"), (61, "ANNUEL", "TIEDE"), (60, None, "CHAUD"), (61, None, "TIEDE"),
    (0, "MENSUEL", "CHAUD"), (-10, None, "CHAUD"),
])
def test_expiry_drives_the_score(days_left, term, expected):
    expiry = (date(2026, 10, 9) + timedelta(days=days_left), term, days_left)
    assert insurance_prospect.score(False, False, None, expiry)[0] == expected


def test_score_rules():
    score = insurance_prospect.score
    assert score(False, False, "SOLD", (date(2026, 10, 10), None, 1)) == ("VENDU", ["contrat souscrit"])
    assert score(True, True, None, None) == ("CHAUD", ["demande de cotation transmise", "appel ou rendez-vous prévu"])
    assert score(True, False, None, None) == ("TIEDE", ["demande de cotation transmise"])
    assert score(False, True, None, None)[0] == "TIEDE" and score(False, False, "FOLLOW_UP", None)[0] == "TIEDE"
    assert score(False, False, None, None) == ("FROID", ["simple renseignement pour l'instant"])
    assert score(False, False, "NO_SHOW", None) == ("FROID", ["absent au rendez-vous"])
    # Pas intéressé au rendez-vous : froid, même avec une échéance proche.
    assert score(True, False, "NOT_INTERESTED", (date(2026, 10, 12), "MENSUEL", 3))[0] == "FROID"
    assert score(False, False, None, (date(2026, 10, 21), "MENSUEL", 12))[1] == ["échéance dans 12 jours (contrat mensuel)"]
    assert insurance_prospect.expiry_reason(1, None) == "échéance dans 1 jour"
    assert insurance_prospect.expiry_reason(0, "ANNUEL") == "échéance aujourd'hui (contrat annuel)"
    assert insurance_prospect.expiry_reason(-1, None) == "contrat actuel échu depuis 1 jour"
    assert insurance_prospect.expiry_reason(-3, None) == "contrat actuel échu depuis 3 jours"


def test_nearest_expiry():
    today = date(2026, 10, 9)
    requests = [QuoteRequest(details={"current_expiry": "2026-12-01", "current_term": "ANNUEL"}),
                QuoteRequest(details={"current_expiry": "2026-10-20", "current_term": "BIZARRE"}),
                QuoteRequest(details={"current_expiry": "fin octobre"}),           # ancienne valeur libre (lot 53)
                QuoteRequest(details={"current_expiry": "2026-08-01"}),           # échu depuis plus de 30 jours
                QuoteRequest(details=None)]
    assert insurance_prospect.nearest_expiry(requests, today) == (date(2026, 10, 20), None, 11)
    assert insurance_prospect.nearest_expiry(requests[2:], today) is None
    assert insurance_prospect.nearest_expiry([QuoteRequest(details={"current_expiry": "2026-09-09"})], today)[2] == -30


@pytest.mark.asyncio
async def test_prospects_of_the_cabinet(db_session):
    cabinet = await _cabinet(db_session)
    other = await _cabinet(db_session)
    hot, conv = await _conversation(db_session, cabinet)
    await insurance.update_request(db_session, cabinet.id, hot.id, conv.id, {
        "branch": "AUTO", "vehicle": "Corolla", "current_insurer": "NSIA", "current_expiry": "2026-10-20",
        "current_term": "MENSUEL"}, NOW)
    warm, warm_conv = await _conversation(db_session, cabinet)
    await insurance.update_request(db_session, cabinet.id, warm.id, warm_conv.id, {
        "branch": "SCOLAIRE", "children_count": 2, "consent": True}, NOW)
    sold, sold_conv = await _conversation(db_session, cabinet)
    db_session.add(AppointmentRequest(tenant_id=cabinet.id, customer_id=sold.id, conversation_id=sold_conv.id, kind="CABINET",
                                      availability="x", status="CONFIRMED", outcome="SOLD", scheduled_at=NOW - timedelta(days=2)))
    o_customer, o_conv = await _conversation(db_session, other)
    await insurance.update_request(db_session, other.id, o_customer.id, o_conv.id, {"branch": "SCOLAIRE", "children_count": 1})
    await db_session.commit()

    prospects = await insurance_prospect.for_customers(db_session, cabinet, None, NOW)
    assert set(prospects) == {hot.id, warm.id, sold.id}  # jamais les clients d'un autre cabinet
    assert prospects[hot.id]["score"] == "CHAUD" and prospects[hot.id]["reasons"] == ["échéance dans 11 jours (contrat mensuel)"]
    assert prospects[hot.id]["expiry_soon"] and prospects[hot.id]["current_insurer"] == "NSIA"
    assert prospects[warm.id]["score"] == "TIEDE" and prospects[warm.id]["branches"] == ["Assurance scolaire"]
    assert prospects[sold.id]["score_label"] == "Souscrit"
    assert await insurance_prospect.for_customers(db_session, cabinet, [], NOW) == {}
    assert set(await insurance_prospect.for_customers(db_session, cabinet, [hot.id], NOW)) == {hot.id}

    from app.services.prospect import build_fiche, hot_alert_emails

    fiche = await build_fiche(db_session, cabinet, hot, NOW)
    assert fiche["lines"][-3:] == ["Assureur actuel : NSIA", "Échéance du contrat actuel : 20/10/2026 (mensuel)",
                                   "Score commercial : Chaud (échéance dans 11 jours (contrat mensuel))"]
    assert "Assurance demandée : Assurance automobile" in fiche["lines"]
    from app.models.prospect_profile import ProspectProfile

    db_session.add(ProspectProfile(tenant_id=cabinet.id, customer_id=hot.id))  # fiche d'un ancien secteur
    await db_session.commit()
    assert await hot_alert_emails(db_session, cabinet, hot, conv) == []  # le score part avec l'email de cotation
    new_customer, _ = await _conversation(db_session, cabinet)
    assert await build_fiche(db_session, cabinet, new_customer, NOW) is None


@pytest.mark.asyncio
async def test_customer_card_shows_the_insurance_score(client, db_session):
    cabinet = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, cabinet)
    await insurance.update_request(db_session, cabinet.id, customer.id, conversation.id,
                                   {"branch": "SCOLAIRE", "children_count": 2, "consent": True})
    await db_session.commit()
    r = await client.get(f"/api/v1/customers/{customer.id}", headers=await _headers(client, cabinet))
    assert r.json()["prospect"]["score"] == "TIEDE" and "Assurance demandée : Assurance scolaire" in r.json()["prospect"]["lines"]


# --- Accord du client (CIMA) ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_appointment_is_consent_to_be_contacted(db_session):
    cabinet = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, cabinet)
    complete = await insurance.update_request(db_session, cabinet.id, customer.id, conversation.id,
                                              {"branch": "HABITATION", "occupancy": "locataire", "housing_type": "villa"})
    partial = await insurance.update_request(db_session, cabinet.id, customer.id, conversation.id, {"branch": "AUTO"})
    assert complete["needs_consent"] and not partial["needs_consent"]
    executor = ToolExecutor(db_session, cabinet.id, conversation, business_type=INSURANCE_BROKER)

    r = await executor.execute("request_appointment", {"kind": "APPEL", "availability": "lundi 12 octobre matin"})

    assert r["status"] == "appointment_requested"
    assert complete["request"].status == "SUBMITTED" and complete["request"].consent_at is not None
    assert partial["request"].status == "DRAFT" and partial["request"].consent_at is not None  # accord noté, minimum manquant
    notes = (await db_session.execute(select(Message.content).where(Message.message_type == "quote_request"))).scalars().all()
    assert notes == ["Demande de cotation transmise au cabinet : Assurance habitation"]
    # Le minimum arrive ensuite : la demande part, sans redemander l'accord.
    later = await insurance.update_request(db_session, cabinet.id, customer.id, conversation.id,
                                           {"branch": "AUTO", "vehicle": "Corolla", "vehicle_year": 2019, "usage": "privé"})
    assert later["submitted_now"] and not later["needs_consent"]


@pytest.mark.asyncio
async def test_consent_from_appointment_only_for_the_cabinet(db_session):
    dealer = await _cabinet(db_session, business_type=CAR_DEALERSHIP)
    customer, conversation = await _conversation(db_session, dealer)
    db_session.add(QuoteRequest(tenant_id=dealer.id, customer_id=customer.id, branch="SCOLAIRE", client_type="PARTICULIER",
                                details={"children_count": 1}, status="DRAFT"))
    await db_session.commit()
    executor = ToolExecutor(db_session, dealer.id, conversation, business_type=CAR_DEALERSHIP)
    await executor.execute("request_appointment", {"kind": "ESSAI", "availability": "lundi 12 octobre matin"})
    assert (await db_session.execute(select(QuoteRequest.status))).scalar_one() == "DRAFT"


@pytest.mark.asyncio
async def test_consent_is_never_taken_from_a_false_value(db_session):
    cabinet = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, cabinet)
    for value in (False, "true", 1, None):
        result = await insurance.update_request(db_session, cabinet.id, customer.id, conversation.id,
                                                {"branch": "SCOLAIRE", "children_count": 1, "consent": value})
        assert result["request"].consent_at is None and result["needs_consent"], value


@pytest.mark.asyncio
async def test_quote_email_carries_the_score_and_the_trace(db_session):
    cabinet = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, cabinet)
    await insurance.update_request(db_session, cabinet.id, customer.id, conversation.id, {
        "branch": "AUTO", "vehicle": "Corolla", "vehicle_year": 2019, "usage": "taxi", "current_expiry": "2026-10-15",
        "current_term": "MENSUEL", "budget": "50 000 par mois", "payment_wish": "en deux fois", "consent": True}, NOW)
    await db_session.commit()

    [mail] = await insurance.quote_alert_emails(db_session, cabinet, customer, conversation, NOW)

    assert mail["subject"].startswith("🔥 Nouvelle demande de cotation — Assurance automobile")
    body = mail["body"]
    assert "Échéance du contrat actuel : 15/10/2026" in body and "Durée du contrat actuel : Mensuel" in body
    assert "Budget envisagé par le client : 50 000 par mois" in body and "Souhait du client pour le paiement : en deux fois" in body
    assert "Score commercial : Chaud (échéance dans 6 jours (contrat mensuel), demande de cotation transmise)" in body
    assert "Accord du client pour la transmission : 09/10/2026 à 10:00 (UTC)" in body
    assert "Transmise au cabinet : 09/10/2026 à 10:00 (UTC)" in body
    assert await insurance.quote_alert_emails(db_session, cabinet, customer, conversation, NOW) == []  # une seule fois


def test_display_and_trace():
    assert insurance.display_value("current_expiry", "2026-10-15") == "15/10/2026"
    assert insurance.display_value("current_expiry", "fin octobre") == "fin octobre"  # ancienne valeur, telle quelle
    assert insurance.display_value("current_term", "TRIMESTRIEL") == "Trimestriel"
    assert insurance.display_value("vehicle_year", 2019) == "2019"
    request = QuoteRequest(consent_at=datetime(2026, 10, 9, 9, 5), submitted_at=None, handled_at=None)
    assert insurance.trace_lines(request) == ["Accord du client pour la transmission : 09/10/2026 à 09:05 (UTC)"]
    assert insurance.parse_date(date(2026, 1, 2)) == date(2026, 1, 2) and insurance.parse_date(20261002) is None


def test_tool_definition_asks_for_dates_and_consent():
    [tool] = [t for t in TOOL_DEFINITIONS if t["name"] == "update_insurance_request"]
    props = tool["input_schema"]["properties"]
    assert props["consent"]["type"] == "boolean" and "accepter clairement" in props["consent"]["description"]
    assert props["current_term"]["enum"] == ["MENSUEL", "TRIMESTRIEL", "SEMESTRIEL", "ANNUEL"]
    assert "AAAA-MM-JJ" in props["current_expiry"]["description"] and "ne le répète jamais" in props["budget"]["description"]
    assert "accord du client" in tool["description"]


@pytest.mark.asyncio
async def test_quotes_page_shows_score_and_trace(client, db_session):
    cabinet = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, cabinet)
    sent = await insurance.update_request(db_session, cabinet.id, customer.id, conversation.id,
                                          {"branch": "SCOLAIRE", "children_count": 2, "consent": True})
    await db_session.commit()
    headers = await _headers(client, cabinet)
    [row] = (await client.get("/api/v1/quote-requests", headers=headers)).json()
    assert row["prospect"]["score"] == "TIEDE" and row["consent_at"] is not None
    assert [line.split(" : ")[0] for line in row["trace"]] == ["Accord du client pour la transmission", "Transmise au cabinet"]
    handled = (await client.post(f"/api/v1/quote-requests/{sent['request'].id}/handle", headers=headers)).json()
    assert handled["trace"][-1].startswith("Prise en charge : ") and handled["prospect"]["score"] == "TIEDE"
    body = _function("loadQuotes")
    assert 'scorePill(q.prospect)' in body and "${q.trace.map(esc).join(\" · \")}" in body
    assert "l'accord du client" in body and "${esc(q.prospect.reasons.join(\", \"))}" in body


# --- Statut du cabinet, prompt, réglages ------------------------------------------------------------------

def test_identity_follows_the_structure():
    def tenant(structure=None, insurer=None, license_=None, complaints=None):
        return Tenant(name="Assur Plus", country="CI", currency="XOF", email="a@b.ci", business_type=INSURANCE_BROKER,
                      insurance_structure=structure, insurer_name=insurer, insurance_license=license_,
                      complaints_contact=complaints)

    assert insurance_identity(tenant()) == "intermédiaire en assurance"
    assert insurance_identity(tenant("COURTIER", "Sunu")) == "cabinet de courtage en assurance"
    assert insurance_identity(tenant("AGENCE_GENERALE", "Sunu")) == "agence générale d'assurance Sunu"
    assert insurance_identity(tenant("AGENCE_GENERALE")) == "agence générale d'assurance"
    assert insurance_identity(tenant("AGENT", "NSIA")) == "agent d'assurance mandataire de NSIA"
    assert insurance_identity(tenant("AGENT")) == "agent d'assurance"
    assert insurance_office_info(tenant()) == "Aucune information renseignée par le cabinet."
    info = insurance_office_info(tenant("AGENT", "NSIA", "  A-12   34 ", "reclamations@assur.ci"))
    assert info.split("\n") == ["Statut : Agent d'assurance", "Compagnie d'assurance mandante : NSIA",
                                "Numéro d'agrément : A-12 34", "Contact pour les réclamations : reclamations@assur.ci"]

    prompt = build_system_prompt(tenant("AGENT", "NSIA"), now=NOW)
    assert "Tu es Bob, l'assistant virtuel de Assur Plus, agent d'assurance mandataire de NSIA." in prompt
    assert "parle de « l'agence »" in prompt and "INFORMATIONS DU CABINET\nStatut : Agent d'assurance" in prompt
    for rule in ("15. Si le client demande qui vous êtes", "16. Réclamation ou mécontentement",
                 "17. Paiement de la prime : ne propose JAMAIS de crédit", "ne transmets\n   JAMAIS sans cet accord",
                 "sa durée (mensuel,\n   trimestriel, semestriel ou annuel)"):
        assert rule in prompt, rule
    assert "parle de « l'agence »" in build_system_prompt(tenant("AGENCE_GENERALE"), now=NOW)
    courtier = build_system_prompt(tenant("COURTIER"), now=NOW)
    assert "l'agence" not in courtier and "cabinet de courtage en assurance." in courtier
    assert "cabinet de courtage / agence" not in courtier


@pytest.mark.asyncio
async def test_insurance_profile_settings(client, db_session):
    cabinet = await _cabinet(db_session)
    headers = await _headers(client, cabinet)
    empty = (await client.get("/api/v1/tenants/me/insurance-profile", headers=headers)).json()
    assert empty["insurance_structure"] is None and [o["code"] for o in empty["options"]] == ["COURTIER", "AGENCE_GENERALE", "AGENT"]
    assert empty["preview"] == "Je suis Bob, l'assistant virtuel de Cabinet Kouassi, intermédiaire en assurance."

    r = await client.put("/api/v1/tenants/me/insurance-profile", headers=headers, json={
        "insurance_structure": "AGENT", "insurer_name": "  NSIA   Assurances ", "insurance_license": " ",
        "complaints_contact": "reclamations@kouassi.ci"})
    assert r.status_code == 200 and r.json()["insurer_name"] == "NSIA Assurances" and r.json()["insurance_license"] is None
    assert r.json()["preview"].endswith("agent d'assurance mandataire de NSIA Assurances.")
    await db_session.refresh(cabinet)
    assert cabinet.complaints_contact == "reclamations@kouassi.ci"

    courtier = await client.put("/api/v1/tenants/me/insurance-profile", headers=headers, json={
        "insurance_structure": "COURTIER", "insurer_name": "NSIA", "insurance_license": "B-55"})
    assert courtier.json()["insurer_name"] is None and courtier.json()["insurance_license"] == "B-55"
    assert courtier.json()["complaints_contact"] is None

    bad = await client.put("/api/v1/tenants/me/insurance-profile", headers=headers, json={"insurance_structure": "MUTUELLE"})
    assert bad.status_code == 422
    too_long = await client.put("/api/v1/tenants/me/insurance-profile", headers=headers,
                                json={"insurance_structure": "AGENT", "insurer_name": "x" * 151})
    assert too_long.status_code == 422

    agent = await _cabinet(db_session, role=Role.AGENT)
    refused = await client.put("/api/v1/tenants/me/insurance-profile", headers=await _headers(client, agent),
                               json={"insurance_structure": "COURTIER"})
    assert refused.status_code == 403
    store = await _cabinet(db_session, business_type=ONLINE_STORE)
    assert (await client.get("/api/v1/tenants/me/insurance-profile", headers=await _headers(client, store))).status_code == 403


@pytest.mark.asyncio
async def test_onboarding_asks_the_cabinet_for_its_structure(client, db_session):
    cabinet = await _cabinet(db_session)
    steps = (await client.get("/api/v1/tenants/me/onboarding", headers=await _headers(client, cabinet))).json()["steps"]
    structure = next(s for s in steps if s["key"] == "structure")
    assert structure["tab"] == "bob" and not structure["done"] and len(steps) == 6
    cabinet.insurance_structure = "COURTIER"
    await db_session.commit()
    steps = (await client.get("/api/v1/tenants/me/onboarding", headers=await _headers(client, cabinet))).json()["steps"]
    assert next(s for s in steps if s["key"] == "structure")["done"]
    for other in (ONLINE_STORE, CAR_DEALERSHIP):
        tenant = await _cabinet(db_session, business_type=other)
        keys = [s["key"] for s in (await client.get("/api/v1/tenants/me/onboarding", headers=await _headers(client, tenant))).json()["steps"]]
        assert "structure" not in keys and len(keys) == 5


def test_ignored_handoff_choices_are_hidden_for_the_cabinet():
    assert '<select id="handoff-complaint" class="input no-insurance"' in HTML
    assert '<select id="handoff-discount" class="input no-insurance"' in HTML
    assert "Une réclamation ou un sinistre est toujours transmis au cabinet" in HTML


def test_dashboard_insurance_profile_card():
    assert 'class="card insurance-only" style="max-width: 480px; flex: 1; min-width: 300px;" id="insurance-profile-card"' in HTML
    assert 'if (currentBusinessType === "INSURANCE_BROKER") loadInsuranceProfile();' in HTML
    load = _function("loadInsuranceProfile")
    assert "${esc(o.label)}" in load and 'value="${esc(o.code)}"' in load and "${esc(e.message)}" in load
    changed = _function("insuranceStructureChanged")
    assert 'classList.toggle("hidden", chosen === "COURTIER")' in changed and "${esc(insuranceProfile.preview)}" in changed
    save = _function("saveInsuranceProfile")
    assert '"/api/v1/tenants/me/insurance-profile"' in save and 'method: "PUT"' in save


# --- Accueil du courtier ----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_home_of_the_cabinet(db_session):
    from app.services.home_service import home_summary

    cabinet = await _cabinet(db_session)
    soon, conv = await _conversation(db_session, cabinet)
    await insurance.update_request(db_session, cabinet.id, soon.id, conv.id, {
        "branch": "AUTO", "vehicle": "Corolla", "vehicle_year": 2019, "usage": "privé", "current_expiry": "2026-10-20",
        "current_term": "MENSUEL", "consent": True}, NOW - timedelta(minutes=30))
    later, later_conv = await _conversation(db_session, cabinet)
    await insurance.update_request(db_session, cabinet.id, later.id, later_conv.id, {
        "branch": "SCOLAIRE", "children_count": 1, "current_expiry": "2026-11-25", "current_term": "TRIMESTRIEL",
        "current_insurer": "Sunu", "consent": True},
        NOW - timedelta(hours=1))
    far, far_conv = await _conversation(db_session, cabinet)
    await insurance.update_request(db_session, cabinet.id, far.id, far_conv.id,
                                   {"branch": "HABITATION", "current_expiry": "2027-03-01"}, NOW)
    old = QuoteRequest(tenant_id=cabinet.id, customer_id=far.id, branch="VOYAGE", client_type="PARTICULIER", details={},
                       status="HANDLED", submitted_at=NOW - timedelta(days=40))
    db_session.add(old)
    other = await _cabinet(db_session)  # un autre cabinet ne compte jamais
    o_customer, _ = await _conversation(db_session, other)
    db_session.add(QuoteRequest(tenant_id=other.id, customer_id=o_customer.id, branch="AUTO", client_type="PARTICULIER",
                                details={}, status="HANDLED", submitted_at=NOW - timedelta(hours=3)))
    await db_session.commit()

    home = await home_summary(db_session, cabinet.id, now=NOW)

    assert home["kpis"]["quotes"] == {"value": 2, "delta_pct": 100.0, "to_handle": 2}
    assert [f["key"] for f in home["funnel"]] == ["conversations", "quotes", "appointments", "sold"]
    assert home["funnel"][1]["value"] == 2
    assert home["prospect_scores"] == {"CHAUD": 1, "TIEDE": 2, "FROID": 0, "VENDU": 0}
    assert [(e["customer"], e["days_left"], e["hot"], e["term"]) for e in home["expiries"]] == [
        ("Awa", 11, True, "MENSUEL"), ("Awa", 47, False, "TRIMESTRIEL")]
    assert home["expiries"][1]["current_insurer"] == "Sunu" and home["expiries_total"] == 2
    quotes = [t for t in home["todo"] if t["kind"] == "QUOTE"]
    assert [t["reason"] for t in quotes] == ["Prospect chaud", "Demande de cotation"]  # le chaud d'abord
    assert quotes[0]["detail"] == "Assurance automobile · 🔥 échéance dans 11 jours (contrat mensuel), demande de cotation transmise"
    events = [(a["kind"], a["title"]) for a in home["activity"] if a["kind"] == "QUOTE"]
    assert events == [("QUOTE", "Demande de cotation transmise")] * 2
    assert any(d["appointment"] for d in home["daily"])  # jour marqué par la cotation

    store = await _cabinet(db_session, business_type=ONLINE_STORE)
    store_home = await home_summary(db_session, store.id, now=NOW)
    assert "quotes" not in store_home["kpis"] and "expiries" not in store_home and "prospect_scores" not in store_home


@pytest.mark.asyncio
async def test_expiries_skip_closed_prospects(db_session):
    from app.services.home_service import EXPIRY_LIST_LIMIT, home_summary

    cabinet = await _cabinet(db_session)
    sold, conv = await _conversation(db_session, cabinet)
    await insurance.update_request(db_session, cabinet.id, sold.id, conv.id, {"branch": "AUTO", "current_expiry": "2026-10-12"}, NOW)
    db_session.add(AppointmentRequest(tenant_id=cabinet.id, customer_id=sold.id, conversation_id=conv.id, kind="CABINET",
                                      availability="x", status="CONFIRMED", outcome="SOLD", scheduled_at=NOW - timedelta(days=1)))
    for i in range(EXPIRY_LIST_LIMIT + 2):
        customer, c = await _conversation(db_session, cabinet)
        await insurance.update_request(db_session, cabinet.id, customer.id, c.id,
                                       {"branch": "AUTO", "current_expiry": (date(2026, 10, 10) + timedelta(days=i)).isoformat()}, NOW)
    await db_session.commit()
    home = await home_summary(db_session, cabinet.id, now=NOW)
    assert len(home["expiries"]) == EXPIRY_LIST_LIMIT and home["expiries_total"] == EXPIRY_LIST_LIMIT + 2
    assert [e["days_left"] for e in home["expiries"]] == list(range(1, EXPIRY_LIST_LIMIT + 1))
    assert home["prospect_scores"]["VENDU"] == 1


def test_dashboard_home_of_the_cabinet():
    render = _function("renderHome")
    assert "const kpis = insurer ? [" in render and '"Demandes de cotation", `${k.quotes.value}`' in render
    assert '"Rendez-vous et appels"' in render and "const insuranceRow = insurer ? insuranceHomeRow(h) : \"\";" in render
    assert '"De la première question au contrat"' in render and '"Jour avec cotation ou rendez-vous"' in render
    row = _function("insuranceHomeRow")
    for piece in ("${esc(initials(e.customer))}", "${esc(e.customer)}", "openCustomer('${esc(e.customer_id)}')",
                  "Échéances à venir", "15 jours pour un contrat mensuel, 45 pour un trimestriel, 60 sinon"):
        assert piece in row, piece
    assert 'shield: "M12 3 4 6v6' in HTML


# --- Démo courtier --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_instant_demo_for_the_cabinet(client, db_session):
    r = await client.post("/api/v1/demo/create", data={"company_name": " Assur Conseil ", "business_type": INSURANCE_BROKER,
                                                       "country": "CI", "currency": "XOF", "insurance_structure": "AGENCE_GENERALE"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 11 and body["available"] == 11 and body["failed"] == 0
    tenant = await db_session.get(Tenant, uuid.UUID(body["tenant_id"]))
    assert tenant.is_demo and tenant.business_type == INSURANCE_BROKER and tenant.insurance_structure == "AGENCE_GENERALE"
    assert tenant.name == "Assur Conseil"
    products = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id))).scalars().all()
    assert len(products) == 11 and all(float(p.price) == 0 for p in products)
    settings = (await db_session.execute(select(TenantAppointmentSettings).where(
        TenantAppointmentSettings.tenant_id == tenant.id))).scalar_one()
    assert settings.online_booking and settings.slot_minutes == 30

    default = await client.post("/api/v1/demo/create", data={"company_name": "B", "business_type": INSURANCE_BROKER, "country": "SN"})
    assert (await db_session.get(Tenant, uuid.UUID(default.json()["tenant_id"]))).insurance_structure == "COURTIER"
    bad = await client.post("/api/v1/demo/create", data={"company_name": "B", "business_type": INSURANCE_BROKER, "country": "SN",
                                                         "insurance_structure": "MUTUELLE"})
    assert bad.status_code == 400 and "Statut inconnu" in bad.json()["detail"]
    # Les autres secteurs exigent toujours leur fichier.
    store = await client.post("/api/v1/demo/create", data={"company_name": "B", "business_type": ONLINE_STORE, "country": "SN"})
    assert store.status_code == 400 and "fichier catalogue" in store.json()["detail"]


@pytest.mark.asyncio
async def test_promoting_the_demo_drops_its_quote_requests(client, db_session, monkeypatch):
    from app.models.customer import Customer
    from app.services import email_verification

    r = await client.post("/api/v1/demo/create", data={"company_name": "Assur", "business_type": INSURANCE_BROKER, "country": "CI"})
    token, tenant_id = r.json()["demo_token"], uuid.UUID(r.json()["tenant_id"])
    demo_customer = Customer(tenant_id=tenant_id, whatsapp_number="demo-web-session")
    real_customer = Customer(tenant_id=tenant_id, whatsapp_number="2250733333333")
    db_session.add_all([demo_customer, real_customer])
    await db_session.flush()
    for customer in (demo_customer, real_customer):
        db_session.add(QuoteRequest(tenant_id=tenant_id, customer_id=customer.id, branch="AUTO", client_type="PARTICULIER",
                                    details={}, status="SUBMITTED"))
    await db_session.commit()

    async def ok(*a, **kw):
        return True

    monkeypatch.setattr(email_verification, "check_code", ok)
    promoted = await client.post("/api/v1/demo/promote", headers={"Authorization": f"Bearer {token}"}, json={
        "email": f"p{uuid.uuid4().hex[:6]}@assur.ci", "password": "motdepasse", "full_name": "Awa", "verification_code": "123456"})
    assert promoted.status_code == 200, promoted.text
    left = (await db_session.execute(select(QuoteRequest.customer_id).where(QuoteRequest.tenant_id == tenant_id))).scalars().all()
    assert left == [real_customer.id]


def test_instant_demo_page_offers_the_cabinet():
    assert "chooseSector('INSURANCE_BROKER')" in DEMO_HTML and "🛡️ Courtier / agent d'assurance" in DEMO_HTML
    assert 'id="structure-field"' in DEMO_HTML and '<option value="AGENCE_GENERALE">Agence générale</option>' in DEMO_HTML
    choose = _function("chooseSector", DEMO_HTML)
    assert 'document.getElementById("catalog-field").classList.toggle("hidden", !!sector.noCatalog);' in choose
    create = _function("createDemo", DEMO_HTML.replace("async function createDemo", "function createDemo"))
    assert 'if (noCatalog) formData.append("insurance_structure"' in create
    assert "if (!selectedFile && !noCatalog)" in create
    assert '"J\'ai déjà une assurance, mon contrat finit dans 10 jours"' in DEMO_HTML and "bientôt" not in DEMO_HTML


# --- Divers -----------------------------------------------------------------------------------------------

def test_migration_exists():
    text = (ROOT.parent / "alembic" / "versions" / "d54b3f8a2c17_lot_54_courtier_structure_consentement.py").read_text(encoding="utf-8")
    assert "down_revision = 'c53a8e2f4b61'" in text
    for column in ("insurance_structure", "insurer_name", "insurance_license", "complaints_contact", "consent_at"):
        assert f"'{column}'" in text, column


@pytest.mark.asyncio
async def test_quote_requests_are_counted_once(db_session):
    cabinet = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, cabinet)
    for _ in range(3):
        await insurance.update_request(db_session, cabinet.id, customer.id, conversation.id,
                                       {"branch": "SCOLAIRE", "children_count": 2, "consent": True})
    assert (await db_session.execute(select(func.count(QuoteRequest.id)))).scalar_one() == 1


@pytest.mark.asyncio
async def test_whatsapp_flow_with_consent(client, db_session, wire):  # noqa: F811
    tenant = await _cabinet(db_session)
    state = wire({"intents": ["INTENTION_ACHAT"], "objections": []}, [
        tool_use_response("update_insurance_request", {"branch": "SCOLAIRE", "children_count": 2}),
        text_response("Puis-je transmettre votre demande à votre conseiller ?"),
        tool_use_response("update_insurance_request", {"branch": "SCOLAIRE", "consent": True}),
        text_response("C'est transmis ! Préférez-vous un appel ou un rendez-vous ?"),
    ])
    await client.post("/webhooks/whatsapp", json=_payload(tenant.pnid, "2250744444444", "Assurance scolaire pour 2 enfants"))
    assert not [m for m in state["outbox"] if "cotation" in m["subject"]]  # pas d'accord : rien ne part
    await client.post("/webhooks/whatsapp", json=_payload(tenant.pnid, "2250744444444", "Oui allez-y"))
    [mail] = [m for m in state["outbox"] if "cotation" in m["subject"]]
    assert "Accord du client pour la transmission" in mail["body"] and "Score commercial : Tiède" in mail["body"]


@pytest.mark.parametrize("text,promise", [
    ("Puis-je transmettre votre demande à votre conseiller ?", False),
    ("Acceptez-vous que je transmette votre demande au conseiller du cabinet, pour qu'il vous prépare une proposition ?", False),
    ("Merci ! Vous voulez que je transmette votre demande à un conseiller ?", False),
    ("Je peux transmettre votre demande au conseiller ?", False),
    ("Souhaitez-vous qu'un conseiller vous rappelle ?", False),
    ("Préférez-vous qu'un conseiller vous appelle ou venir au cabinet ?", False),
    # Toujours des promesses : affirmation, ou question qui n'en est pas une.
    ("Je transmets votre demande au conseiller.", True),
    ("Un conseiller va vous rappeler, d'accord ?", True),
    ("Je transmets votre demande à un conseiller ?", True),
    ("Puis-je vous aider ? Un conseiller va vous rappeler.", True),
    ("Je peux transmettre votre demande à un conseiller.", True),
])
def test_a_consent_question_is_not_a_promise(text, promise):
    from app.services.promise_guard import contains_human_promise

    assert contains_human_promise(text) is promise


@pytest.mark.asyncio
async def test_a_request_sent_before_lot_54_never_asks_for_consent_again(db_session):
    cabinet = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, cabinet)
    legacy = QuoteRequest(tenant_id=cabinet.id, customer_id=customer.id, conversation_id=conversation.id, branch="SCOLAIRE",
                          client_type="PARTICULIER", details={"children_count": 2}, status="SUBMITTED", submitted_at=NOW)
    db_session.add(legacy)
    await db_session.commit()
    result = await ToolExecutor(db_session, cabinet.id, conversation, business_type=INSURANCE_BROKER).execute(
        "update_insurance_request", {"branch": "SCOLAIRE", "ages": "6 et 9 ans"})
    assert result["status"] == "quote_request_updated" and legacy.consent_at is None
