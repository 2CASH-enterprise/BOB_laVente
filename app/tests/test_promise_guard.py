"""
Lot 16 — « Promesses tenues ».

Incidents réels du 26/09/2026 rejoués ici :
- Bob écrit « Je transmets ta préoccupation à un conseiller, qui te contactera rapidement »
  sans appeler l'outil de transfert : personne n'était prévenu ;
- « Vous acceptez les retours ? » avec une base de connaissances vide : Bob invente, ou promet
  de « vérifier auprès de la boutique » sans que la boutique ne le sache.
"""
import pytest
from sqlalchemy import select

from app.agents.classifier import build_classifier_prompt
from app.agents.orchestrator import FALLBACK_MESSAGE
from app.agents.prompts import BASE_RULES, _format_missing_conditions
from app.agents.strategies import NO_KNOWLEDGE_INSTRUCTION
from app.agents.taxonomy import INTENTS, intent_label
from app.models.conversation import ConversationStatus, Message
from app.models.knowledge_entry import KnowledgeCategory, KnowledgeEntry
from app.services.handoff_rules import (
    ALLOW_TRANSFER,
    MISSING_CONDITIONS_MESSAGE,
    TRANSFER_MESSAGE,
    TRANSFER_NOW,
    HandoffSettingsView,
    evaluate,
    transfer_message,
)
from app.services.promise_guard import NEUTRAL_REPLACEMENT, contains_human_promise, remove_human_promises
from app.tests.fakes import text_response, tool_use_response
from app.tests.test_handoff_rules import _conversation, _payload, _setup, _signal, _system_messages, sig, wire  # noqa: F401

DEFAULTS = HandoffSettingsView()

# --- Détection : les vraies phrases de Bob, et les pièges ---------------------------------

REAL_PROMISES = [
    "Je transmets ta préoccupation à un conseiller spécialisé, qui te contactera rapidement pour en discuter.",
    "Un conseiller va reprendre la conversation avec toi très vite.",
    "Je vais vérifier cela pour vous auprès de la boutique. Un instant, s'il vous plaît.",
    "Je transmets votre demande à un conseiller spécialisé qui reprendra contact avec vous.",
    "Quelqu’un de notre équipe va vous recontacter demain.",  # apostrophe typographique
    "La boutique vous répondra dans la journée.",
    "Je transmets votre question à la boutique.",
    "Je me renseigne auprès de l'équipe et je reviens vers vous.",
    "Notre équipe reviendra vers vous rapidement.",
    "Le responsable vous rappellera ce soir.",
    "Je transfère ta demande au responsable.",
    "Je transmets ça à l'équipe.",
    "Un conseiller va vous contacter.",  # espace insécable
    "Quelqu’un va vous recontacter.",  # apostrophe typographique, seule indication d'une personne
    "Je vérifie auprès de l’équipe.",
]

NOT_PROMISES = [
    "Nos prix sont fixes, mais je peux vous proposer un modèle moins cher.",
    "Qu'est-ce qui te fait hésiter ? Le prix, la taille ou la livraison ?",
    "Quel budget envisagez-vous ?",
    "La robe wax reste disponible en taille M.",
    "Je vous transmets le lien de paiement : https://pay.example/x",
    "Je vous transmets le lien de paiement de la boutique : https://pay.example/x",
    "Je te transmets l'adresse de la boutique à Cocody.",
    "Notre conseiller en image vous le recommande avec ces chaussures.",
    "Non, nous ne livrons pas encore à Bouaké.",
    "Je reste disponible si tu as d'autres questions 😊",
    "La boutique propose aussi des sacs assortis.",
    "Vous pouvez contacter la boutique au 0700000000.",
    "Je vous envoie les détails de la commande.",
    "",
]


@pytest.mark.parametrize("text", REAL_PROMISES)
def test_real_promises_are_detected(text):
    assert contains_human_promise(text)


@pytest.mark.parametrize("text", NOT_PROMISES)
def test_ordinary_sentences_are_not_promises(text):
    assert not contains_human_promise(text)


def test_none_is_not_a_promise():
    assert not contains_human_promise(None)


def test_removal_keeps_the_rest_and_the_paragraphs():
    text = "Nos prix sont fixes. Je transmets votre demande à un conseiller.\n\nUn instant, s'il vous plaît.\nAutre chose ?"
    assert remove_human_promises(text) == "Nos prix sont fixes.\n\nAutre chose ?"


def test_removal_never_leaves_an_empty_message():
    assert remove_human_promises("Je vais vérifier auprès de la boutique. Un instant.") == NEUTRAL_REPLACEMENT


def test_text_without_promise_is_returned_untouched():
    text = "Bonjour !\n\nLa robe est à 15 000 XOF. Un instant de réflexion ?"
    assert remove_human_promises(text) == text


# --- Règle : conditions de vente non renseignées -----------------------------------------

def test_sales_conditions_question_without_knowledge_is_transferred():
    decision = evaluate(sig("CONDITIONS_VENTE"), DEFAULTS, False, known_categories=set())
    assert (decision.mode, decision.rule) == (TRANSFER_NOW, "MISSING_CONDITIONS")


@pytest.mark.parametrize("category", ["RETOUR", "GARANTIE", "CONDITIONS"])
def test_sales_conditions_question_with_knowledge_is_answered_by_bob(category):
    decision = evaluate(sig("CONDITIONS_VENTE"), DEFAULTS, False, known_categories={category})
    assert decision.mode == ALLOW_TRANSFER and decision.rule is None


def test_unrelated_knowledge_does_not_answer_sales_conditions():
    decision = evaluate(sig("CONDITIONS_VENTE"), DEFAULTS, False, known_categories={"LIVRAISON", "PAIEMENT", "HORAIRES"})
    assert decision.rule == "MISSING_CONDITIONS"


def test_unknown_knowledge_keeps_previous_behaviour():
    assert evaluate(sig("CONDITIONS_VENTE"), DEFAULTS, False).rule is None


def test_rule_priorities_around_sales_conditions():
    assert evaluate(sig("CONDITIONS_VENTE", "DEMANDE_HUMAIN"), DEFAULTS, False, known_categories=set()).rule == "HUMAN_REQUEST"
    assert evaluate(sig("CONDITIONS_VENTE", "REMBOURSEMENT"), DEFAULTS, False, known_categories=set()).rule == "REFUND"
    no_refund = HandoffSettingsView(refund_transfer=False)
    assert evaluate(sig("CONDITIONS_VENTE", "REMBOURSEMENT"), no_refund, False, known_categories=set()).rule == "MISSING_CONDITIONS"
    # Avant la réclamation et la remise : aucune de ces règles ne doit laisser Bob inventer une condition.
    assert evaluate(sig("CONDITIONS_VENTE", "RECLAMATION"), DEFAULTS, False, known_categories=set()).rule == "MISSING_CONDITIONS"
    assert evaluate(sig("CONDITIONS_VENTE", "DEMANDE_REMISE"), DEFAULTS, False, known_categories=set()).rule == "MISSING_CONDITIONS"


def test_transfer_messages():
    assert transfer_message("MISSING_CONDITIONS") == MISSING_CONDITIONS_MESSAGE
    assert transfer_message("HUMAN_REQUEST") == TRANSFER_MESSAGE
    assert transfer_message(None) == TRANSFER_MESSAGE


# --- Consignes : plus jamais « je vais vérifier » sans transmission -----------------------

def test_instructions_ask_for_a_real_transfer_instead_of_checking():
    missing = _format_missing_conditions([])
    for text in (BASE_RULES, missing, NO_KNOWLEDGE_INSTRUCTION):
        assert "handoff_to_human" in text
        assert "vas vérifier" not in text and "vais vérifier" not in text
    assert "Ne promets JAMAIS qu'un conseiller" in BASE_RULES


def test_fallback_message_no_longer_promises_a_human():
    assert not contains_human_promise(FALLBACK_MESSAGE)


def test_taxonomy_and_classifier_know_sales_conditions():
    assert intent_label("CONDITIONS_VENTE") == "Question sur les conditions de vente"
    assert "politique de retour" in INTENTS["REMBOURSEMENT"][1]
    prompt = build_classifier_prompt()
    assert '« Vous acceptez les retours ? » → {"intents": ["CONDITIONS_VENTE"]' in prompt
    assert "jamais REMBOURSEMENT ni RECLAMATION" in prompt


# --- Webhook : incidents rejoués ----------------------------------------------------------

async def _messages_of_type(db_session, tenant_id, message_type):
    return (await db_session.execute(select(Message.content).where(
        Message.tenant_id == tenant_id, Message.message_type == message_type))).scalars().all()


@pytest.mark.asyncio
async def test_incident_promise_without_transfer_becomes_a_real_transfer(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-p-1")
    promise = "Je comprends. Je transmets ta préoccupation à un conseiller spécialisé, qui te contactera rapidement."
    state = wire({"intents": ["AUTRE"], "objections": ["HESITATION"]}, [text_response(promise)])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-p-1", "221700000201", "Je vais réfléchir"))

    assert r.json()["ai_reply"] == promise  # la promesse est désormais vraie : on la garde telle quelle
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.WAITING_HUMAN
    label = "Bob a promis un suivi par un humain : transfert automatique"
    assert await _system_messages(db_session, tenant.id) == [f"Transfert vers un humain : Règle — {label}"]
    assert len(state["outbox"]) == 1 and label in state["outbox"][0]["body"]  # commerçant prévenu
    signal = await _signal(db_session, tenant.id)
    assert signal.applied_rule == "PROMISE_KEPT" and signal.strategy is None


@pytest.mark.asyncio
async def test_incident_check_with_the_shop_becomes_a_real_transfer(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-p-2")
    wire({"intents": ["LIVRAISON"], "objections": []},
         [text_response("Je vais vérifier cela pour vous auprès de la boutique. Un instant, s'il vous plaît.")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-p-2", "221700000202", "Vous livrez à Bouaké ?"))

    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.WAITING_HUMAN


@pytest.mark.asyncio
async def test_promise_is_kept_even_without_classification(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-p-3")
    wire(None, [text_response("Un conseiller va reprendre la conversation avec toi.")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-p-3", "221700000203", "Hmm"))

    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.WAITING_HUMAN
    assert len(await _system_messages(db_session, tenant.id)) == 1


@pytest.mark.asyncio
async def test_forbidden_transfer_removes_the_promise_instead(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-p-4", negotiation=False)
    state = wire({"intents": ["DEMANDE_REMISE"], "objections": []},
                 [text_response("Nos prix sont fixes. Je transmets votre demande à un conseiller qui vous recontactera.")])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-p-4", "221700000204", "Vous faites un prix ?"))

    assert r.json()["ai_reply"] == "Nos prix sont fixes."
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.ACTIVE
    assert await _system_messages(db_session, tenant.id) == []
    [trace] = await _messages_of_type(db_session, tenant.id, "promise_removed")
    assert "remise sans négociation : prix fixes" in trace
    assert state["outbox"] == []
    assert (await _signal(db_session, tenant.id)).applied_rule == "DISCOUNT_FIXED_PRICES"


@pytest.mark.asyncio
async def test_real_transfer_by_bob_is_not_doubled(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-p-5")
    state = wire({"intents": ["RECLAMATION"], "objections": []},
                 [tool_use_response("handoff_to_human", {"reason": "Colis abîmé"}),
                  text_response("Je transmets votre demande à un conseiller, qui vous recontactera.")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-p-5", "221700000205", "Mon colis est abîmé"))

    [content] = await _system_messages(db_session, tenant.id)
    assert content.startswith("Transfert vers un humain : Colis abîmé — décision de Bob")
    assert len(state["outbox"]) == 1
    assert (await _signal(db_session, tenant.id)).applied_rule == "COMPLAINT_TRY_FIRST"


@pytest.mark.asyncio
async def test_ordinary_reply_is_untouched(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-p-6")
    reply = "Je vous transmets le lien de paiement de la boutique : https://pay.example/x"
    state = wire({"intents": ["PAIEMENT"], "objections": []}, [text_response(reply)])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-p-6", "221700000206", "Comment je paie ?"))

    assert r.json()["ai_reply"] == reply
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.ACTIVE
    assert state["outbox"] == []


@pytest.mark.asyncio
async def test_incident_returns_question_with_empty_knowledge_is_transmitted(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-p-7")
    state = wire({"intents": ["CONDITIONS_VENTE"], "objections": []}, [])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-p-7", "221700000207", "Vous acceptez les retours ?"))

    assert r.json()["ai_reply"] == MISSING_CONDITIONS_MESSAGE
    assert state["llm"].call_count == 0  # Bob n'a aucune occasion d'inventer une politique de retour
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.WAITING_HUMAN
    label = "conditions de vente non renseignées : question transmise à la boutique"
    assert await _system_messages(db_session, tenant.id) == [f"Transfert vers un humain : Règle — {label}"]
    assert len(state["outbox"]) == 1 and label in state["outbox"][0]["body"]
    assert (await _signal(db_session, tenant.id)).applied_rule == "MISSING_CONDITIONS"


@pytest.mark.asyncio
async def test_returns_question_is_answered_when_the_shop_filled_its_policy(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-p-8")
    db_session.add(KnowledgeEntry(tenant_id=tenant.id, category=KnowledgeCategory.RETOUR, title="Retours",
                                  content="Échange sous 7 jours, article non porté.", active=True))
    await db_session.commit()
    state = wire({"intents": ["CONDITIONS_VENTE"], "objections": []},
                 [text_response("Oui : échange sous 7 jours, si l'article n'a pas été porté.")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-p-8", "221700000208", "Vous acceptez les retours ?"))

    assert state["llm"].call_count == 1
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.ACTIVE


@pytest.mark.asyncio
async def test_inactive_policy_does_not_count(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-p-9")
    db_session.add(KnowledgeEntry(tenant_id=tenant.id, category=KnowledgeCategory.RETOUR, title="Retours",
                                  content="Ancienne politique", active=False))
    await db_session.commit()
    wire({"intents": ["CONDITIONS_VENTE"], "objections": []}, [])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-p-9", "221700000209", "Je peux échanger ?"))

    assert r.json()["ai_reply"] == MISSING_CONDITIONS_MESSAGE


@pytest.mark.asyncio
async def test_another_shops_policy_does_not_count(client, db_session, unique_email, wire):
    other = await _setup(db_session, f"autre-{unique_email}", "pn-p-10b")
    db_session.add(KnowledgeEntry(tenant_id=other.id, category=KnowledgeCategory.RETOUR, title="Retours",
                                  content="Retours sous 30 jours", active=True))
    await db_session.commit()
    tenant = await _setup(db_session, unique_email, "pn-p-10")
    wire({"intents": ["CONDITIONS_VENTE"], "objections": []}, [])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-p-10", "221700000210", "Vous reprenez les articles ?"))

    assert r.json()["ai_reply"] == MISSING_CONDITIONS_MESSAGE
    assert (await _conversation(db_session, tenant.id)).status == ConversationStatus.WAITING_HUMAN
