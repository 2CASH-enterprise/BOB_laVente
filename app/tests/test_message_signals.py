import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.agents.classifier import MessageClassifier, build_classifier_prompt, get_message_classifier, normalize_classification
from app.agents.dependency import get_llm_client
from app.agents.taxonomy import INTENTS, OBJECTIONS
from app.core.security import hash_password
from app.main import app
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.message_signal import MessageSignal
from app.models.order import Order, OrderStatus
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.opportunity_service import recompute_tenant_opportunities
from app.services.signal_service import classify_and_store, signals_summary
from app.tests.fakes import FakeLLMClient, text_response

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


class FakeClassifier(MessageClassifier):
    model_name = "fake-small"

    def __init__(self, result=None, delay=0.0, error=None):
        self.result = result if result is not None else {"intents": ["DEMANDE_REMISE"], "objections": ["PRIX_TROP_ELEVE"], "offered_amount": 250000}
        self.delay = delay
        self.error = error
        self.calls: list[tuple] = []

    async def _call(self, text, previous_shop_message):
        self.calls.append((text, previous_shop_message))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


# --- Validation des réponses du modèle ------------------------------------------------

def test_unknown_codes_and_duplicates_are_dropped():
    out = normalize_classification({"intents": ["DEMANDE_PRIX", "INVENTE", "DEMANDE_PRIX"], "objections": ["PRIX_TROP_ELEVE", "FAUX"], "offered_amount": 1000})
    assert out == {"intents": ["DEMANDE_PRIX"], "objections": ["PRIX_TROP_ELEVE"], "offered_amount": 1000}


@pytest.mark.parametrize("amount", [-5, 0, "250000", True, None, [1]])
def test_invalid_amounts_become_none(amount):
    assert normalize_classification({"intents": ["DEMANDE_REMISE"], "offered_amount": amount})["offered_amount"] is None


def test_no_valid_intent_falls_back_to_other():
    assert normalize_classification({"intents": ["N_IMPORTE_QUOI"]})["intents"] == ["AUTRE"]


@pytest.mark.parametrize("raw", ["texte libre", ["liste"], None, 42])
def test_unusable_answer_is_rejected(raw):
    assert normalize_classification(raw) is None


def test_prompt_lists_every_code_of_the_taxonomy():
    prompt = build_classifier_prompt()
    assert all(code in prompt for code in list(INTENTS) + list(OBJECTIONS))


def test_no_classifier_without_mistral(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "llm_provider", "anthropic")
    assert get_message_classifier() is None


@pytest.mark.asyncio
async def test_timeout_gives_no_classification():
    assert await FakeClassifier(delay=0.5).classify("Bonjour", timeout=0.05) is None


@pytest.mark.asyncio
async def test_model_error_gives_no_classification():
    assert await FakeClassifier(error=RuntimeError("503")).classify("Bonjour") is None


@pytest.mark.asyncio
async def test_empty_message_is_not_sent_to_the_model():
    fake = FakeClassifier()
    assert await fake.classify("   ") is None
    assert fake.calls == []


# --- Mise en place webhook -------------------------------------------------------------

async def _setup(db_session, email: str, phone_number_id: str):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=TenantPlan.INDEPENDANT)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=phone_number_id, system_user_token="t"))
    await db_session.commit()
    return tenant


def _payload(pnid: str, sender: str, text: str, kind="text") -> dict:
    message = {"from": sender, "id": f"wamid.{uuid.uuid4().hex}", "type": kind, "timestamp": "1"}
    if kind == "text":
        message["text"] = {"body": text}
    else:
        message["image"] = {"id": "img1", "mime_type": "image/jpeg"}
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid}, "messages": [message]}}]}]}


@pytest.fixture
def overrides(monkeypatch):
    class _Silent:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, *a, **kw):
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Silent)
    state = {}

    def use(classifier=None, replies=None):
        state["classifier"] = classifier
        app.dependency_overrides[get_message_classifier] = lambda: classifier
        if replies is not None:
            fake = FakeLLMClient(replies)
            state["llm"] = fake
            app.dependency_overrides[get_llm_client] = lambda: fake
        return state

    yield use
    app.dependency_overrides.pop(get_message_classifier, None)
    app.dependency_overrides.pop(get_llm_client, None)


async def _signals(db_session, tenant_id):
    return (await db_session.execute(select(MessageSignal).where(MessageSignal.tenant_id == tenant_id))).scalars().all()


# --- Webhook : stockage, non-blocage, aucun effet sur Bob ------------------------------

@pytest.mark.asyncio
async def test_customer_message_is_classified_and_stored(client, db_session, unique_email, overrides):
    tenant = await _setup(db_session, unique_email, "pn-sig-1")
    classifier = FakeClassifier()
    overrides(classifier, [text_response("Je comprends !")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-sig-1", "221700000001", "C'est trop cher, je vous le prends à 250 000"))

    [signal] = await _signals(db_session, tenant.id)
    assert signal.intents == ["DEMANDE_REMISE"]
    assert signal.objections == ["PRIX_TROP_ELEVE"]
    assert float(signal.offered_amount) == 250000
    assert signal.model == "fake-small" and signal.taxonomy_version == "v1"
    assert classifier.calls[0][0] == "C'est trop cher, je vous le prends à 250 000"


def test_signal_table_never_stores_the_message_text():
    assert not {"content", "text", "body"} & set(MessageSignal.__table__.columns.keys())


@pytest.mark.asyncio
async def test_previous_shop_message_is_given_as_context(client, db_session, unique_email, overrides):
    await _setup(db_session, unique_email, "pn-sig-2")
    classifier = FakeClassifier()
    overrides(classifier, [text_response("Le Samsung A56 est à 280 000 XOF."), text_response("Très bien.")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-sig-2", "221700000002", "Combien le A56 ?"))
    await client.post("/webhooks/whatsapp", json=_payload("pn-sig-2", "221700000002", "Ok"))

    assert classifier.calls[0][1] is None
    assert classifier.calls[1] == ("Ok", "Le Samsung A56 est à 280 000 XOF.")


@pytest.mark.asyncio
async def test_classification_does_not_change_bobs_prompt(client, db_session, unique_email, overrides):
    """Lot 12 : on observe seulement. Rien de la classification ne doit atteindre Bob."""
    await _setup(db_session, unique_email, "pn-sig-3")
    state = overrides(FakeClassifier(), [text_response("Bonjour !")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-sig-3", "221700000003", "Vous faites une remise ?"))

    assert "DEMANDE_REMISE" not in state["llm"].received_systems[0]
    assert "PRIX_TROP_ELEVE" not in repr(state["llm"].received_messages[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("classifier", [FakeClassifier(error=RuntimeError("503")), FakeClassifier(delay=10), None])
async def test_bob_still_replies_when_classification_is_unavailable(client, db_session, unique_email, overrides, monkeypatch, classifier):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "classifier_timeout_seconds", 0.05)
    tenant = await _setup(db_session, unique_email, "pn-sig-4")
    overrides(classifier, [text_response("Bonjour, que puis-je pour vous ?")])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-sig-4", "221700000004", "Bonjour"))

    assert r.json()["ai_reply"] == "Bonjour, que puis-je pour vous ?"
    assert await _signals(db_session, tenant.id) == []


@pytest.mark.asyncio
async def test_storage_failure_never_blocks_bob(client, db_session, unique_email, overrides, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("écriture impossible")

    monkeypatch.setattr("app.services.signal_service.MessageSignal", broken)
    await _setup(db_session, unique_email, "pn-sig-5")
    overrides(FakeClassifier(), [text_response("Bonjour !")])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-sig-5", "221700000005", "Bonjour"))

    assert r.json()["ai_reply"] == "Bonjour !"


@pytest.mark.asyncio
async def test_failed_write_keeps_the_session_usable(db_session, unique_email):
    """Point de sauvegarde : un échec d'écriture n'invalide pas la conversation en mémoire."""
    tenant = await _setup(db_session, unique_email, "pn-sig-6")
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000006")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.flush()
    message = Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                      message_type="text", content="Bonjour", created_at=NOW)
    db_session.add(message)
    await db_session.commit()

    first = await classify_and_store(db_session, FakeClassifier(), message)
    second = await classify_and_store(db_session, FakeClassifier(), message)  # même message : contrainte d'unicité

    assert first is not None and second is None
    assert conversation.status == ConversationStatus.ACTIVE  # toujours lisible, sans rechargement
    assert len(await _signals(db_session, tenant.id)) == 1


@pytest.mark.asyncio
async def test_messages_are_classified_even_while_waiting_for_a_human(client, db_session, unique_email, overrides):
    tenant = await _setup(db_session, unique_email, "pn-sig-7")
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000007")
    db_session.add(customer)
    await db_session.flush()
    db_session.add(Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.WAITING_HUMAN))
    await db_session.commit()
    overrides(FakeClassifier({"intents": ["DEMANDE_HUMAIN"], "objections": []}), [])

    await client.post("/webhooks/whatsapp", json=_payload("pn-sig-7", "221700000007", "Allô, il y a quelqu'un ?"))

    [signal] = await _signals(db_session, tenant.id)
    assert signal.intents == ["DEMANDE_HUMAIN"]


@pytest.mark.asyncio
async def test_non_text_messages_are_not_classified(client, db_session, unique_email, overrides):
    tenant = await _setup(db_session, unique_email, "pn-sig-8")
    classifier = FakeClassifier()
    overrides(classifier, [text_response("Merci pour la photo !")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-sig-8", "221700000008", "", kind="image"))

    assert classifier.calls == []
    assert await _signals(db_session, tenant.id) == []


# --- Lecture : conversation et résumé --------------------------------------------------

async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_conversation_detail_shows_labels_under_customer_messages(client, db_session, unique_email, overrides):
    tenant = await _setup(db_session, unique_email, "pn-sig-9")
    overrides(FakeClassifier(), [text_response("Je regarde ce que je peux faire.")])
    await client.post("/webhooks/whatsapp", json=_payload("pn-sig-9", "221700000009", "Trop cher, 250 000 ?"))
    conversation = (await db_session.execute(select(Conversation).where(Conversation.tenant_id == tenant.id))).scalar_one()

    r = await client.get(f"/api/v1/conversations/{conversation.id}", headers=await _headers(client, unique_email))

    by_sender = {m["sender"]: m for m in r.json()["messages"]}
    assert by_sender["CUSTOMER"]["signals"] == {
        "intents": [{"code": "DEMANDE_REMISE", "label": "Demande de remise"}],
        "objections": [{"code": "PRIX_TROP_ELEVE", "label": "Prix trop élevé"}],
        "offered_amount": 250000.0,
    }
    assert by_sender["AI"]["signals"] is None


async def _conversation_with_signals(db_session, tenant, number, when, signals, order_status=None):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=number)
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.flush()
    for i, (intents, objections) in enumerate(signals):
        message = Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                          message_type="text", content="…", created_at=when + timedelta(minutes=i))
        db_session.add(message)
        await db_session.flush()
        db_session.add(MessageSignal(tenant_id=tenant.id, conversation_id=conversation.id, message_id=message.id,
                                     intents=intents, objections=objections, model="fake", taxonomy_version="v1",
                                     message_created_at=message.created_at))
    if order_status:
        db_session.add(Order(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id,
                             status=order_status, total_amount=1000, currency="XOF", created_at=when + timedelta(hours=1)))
    await db_session.commit()


@pytest.mark.asyncio
async def test_summary_objection_conversion_uses_opportunities(db_session, unique_email):
    tenant = await _setup(db_session, unique_email, "pn-sig-10")
    old = NOW - timedelta(days=10)
    price = (["DEMANDE_REMISE"], ["PRIX_TROP_ELEVE"])
    await _conversation_with_signals(db_session, tenant, "221700000011", old, [price, price], order_status=OrderStatus.PAID)
    await _conversation_with_signals(db_session, tenant, "221700000012", old, [price])  # abandonnée
    await _conversation_with_signals(db_session, tenant, "221700000013", old, [(["LIVRAISON"], ["CONFIANCE"])], order_status=OrderStatus.PAID)
    await _conversation_with_signals(db_session, tenant, "221700000014", NOW - timedelta(days=1), [price])  # en cours
    await recompute_tenant_opportunities(db_session, tenant.id, now=NOW)

    s = await signals_summary(db_session, tenant.id, now=NOW)
    objections = {o["code"]: o for o in s["objections"]}

    assert objections["PRIX_TROP_ELEVE"]["messages"] == 4
    assert objections["PRIX_TROP_ELEVE"]["opportunities"] == 3  # 2 messages dans la même opportunité = 1
    assert objections["PRIX_TROP_ELEVE"]["terminated"] == 2       # l'opportunité en cours ne compte pas
    assert objections["PRIX_TROP_ELEVE"]["paid"] == 1
    assert objections["PRIX_TROP_ELEVE"]["conversion_rate_pct"] == 50.0
    assert objections["CONFIANCE"]["conversion_rate_pct"] == 100.0
    assert s["objections"][0]["code"] == "PRIX_TROP_ELEVE"  # trié par nombre d'opportunités
    assert s["intents"][0] == {"code": "DEMANDE_REMISE", "label": "Demande de remise", "messages": 4, "opportunities": 3}
    assert s["classified_messages"] == 5 and s["customer_messages"] == 5


@pytest.mark.asyncio
async def test_summary_period_and_isolation(db_session):
    tenant_a = await _setup(db_session, "a@sig.sn", "pn-sig-a")
    tenant_b = await _setup(db_session, "b@sig.sn", "pn-sig-b")
    await _conversation_with_signals(db_session, tenant_a, "221700000021", NOW - timedelta(days=40), [(["DEMANDE_PRIX"], [])])
    await _conversation_with_signals(db_session, tenant_a, "221700000022", NOW - timedelta(days=2), [(["LIVRAISON"], ["DELAI"])])
    await _conversation_with_signals(db_session, tenant_b, "221700000023", NOW - timedelta(days=2), [(["REMBOURSEMENT"], ["QUALITE"])])

    s = await signals_summary(db_session, tenant_a.id, now=NOW)

    assert [i["code"] for i in s["intents"]] == ["LIVRAISON"]
    assert [o["code"] for o in s["objections"]] == ["DELAI"]


@pytest.mark.asyncio
async def test_signals_api(client, db_session, unique_email):
    await _setup(db_session, unique_email, "pn-sig-11")
    r = await client.get("/api/v1/analytics/signals", headers=await _headers(client, unique_email))
    assert r.status_code == 200
    assert r.json() == {"period_days": 30, "classified_messages": 0, "customer_messages": 0, "intents": [], "objections": []}
    assert (await client.get("/api/v1/analytics/signals")).status_code == 401
