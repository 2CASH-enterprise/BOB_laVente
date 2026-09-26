import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.agents.dependency import get_llm_client
from app.core.security import hash_password
from app.main import app
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.negotiation_settings import TenantNegotiationSettings
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.handoff_service import (
    build_handoff_alert,
    customer_display_name,
    history_since_last_transfer,
    reminder_due,
)
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response

T0 = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)


def _msg(sender, content, minutes, message_type="text"):
    return Message(tenant_id=uuid.uuid4(), conversation_id=uuid.uuid4(), sender=sender, message_type=message_type,
                   content=content, created_at=T0 + timedelta(minutes=minutes))


# --- Mémoire de l'IA : découpage au dernier transfert ---------------------------------

def test_history_without_transfer_is_untouched():
    history = [_msg(MessageSender.CUSTOMER, "Bonjour", 0), _msg(MessageSender.AI, "Bienvenue", 1)]
    assert history_since_last_transfer(history) == history


@pytest.mark.parametrize("transfer_type", ["handoff", "takeover", "negotiation_escalated"])
def test_history_starts_after_any_kind_of_transfer(transfer_type):
    old = _msg(MessageSender.CUSTOMER, "Je veux négocier le Samsung A56", 0)
    transfer = _msg(MessageSender.SYSTEM, "Transfert", 1, message_type=transfer_type)
    after = _msg(MessageSender.CUSTOMER, "Bonjour", 2)
    assert history_since_last_transfer([old, transfer, after]) == [after]


def test_history_keeps_human_replies_after_transfer():
    """Bob doit voir ce que le conseiller a répondu (ex. une remise promise) pour ne pas le contredire."""
    history = [
        _msg(MessageSender.CUSTOMER, "Je veux négocier", 0),
        _msg(MessageSender.SYSTEM, "Transfert", 1, message_type="handoff"),
        _msg(MessageSender.HUMAN, "Je vous fais 5 % de remise", 2),
        _msg(MessageSender.SYSTEM, "Rendu à l'IA", 3, message_type="release"),
        _msg(MessageSender.CUSTOMER, "D'accord merci", 4),
    ]
    kept = history_since_last_transfer(history)
    assert [m.content for m in kept] == ["Je vous fais 5 % de remise", "Rendu à l'IA", "D'accord merci"]


def test_only_the_last_transfer_counts():
    history = [
        _msg(MessageSender.SYSTEM, "T1", 0, message_type="handoff"),
        _msg(MessageSender.CUSTOMER, "Entre deux", 1),
        _msg(MessageSender.SYSTEM, "T2", 2, message_type="takeover"),
        _msg(MessageSender.CUSTOMER, "Après", 3),
    ]
    assert [m.content for m in history_since_last_transfer(history)] == ["Après"]


def test_non_transfer_system_messages_do_not_cut():
    history = [_msg(MessageSender.CUSTOMER, "A", 0), _msg(MessageSender.SYSTEM, "Rendu", 1, message_type="release"),
               _msg(MessageSender.CUSTOMER, "B", 2)]
    assert history_since_last_transfer(history) == history


# --- Webhook : mise en place ----------------------------------------------------------

async def _setup(db_session, email: str, phone_number_id: str, plan=TenantPlan.INDEPENDANT):
    tenant = Tenant(name="Boutique Test", country="SN", currency="XOF", email=email, plan=plan)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=phone_number_id, system_user_token="t"))
    product = Product(tenant_id=tenant.id, sku="SAM-A56", name="Samsung A56", price=280000, currency="XOF", stock_quantity=12)
    db_session.add(product)
    await db_session.commit()
    return tenant, product


def _payload(phone_number_id: str, from_number: str, text: str) -> dict:
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_number_id},
        "messages": [{"from": from_number, "id": f"wamid.{uuid.uuid4().hex}", "type": "text",
                      "text": {"body": text}, "timestamp": "1700000000"}],
    }}]}]}


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr("app.api.webhooks.whatsapp.send_email", lambda **kw: sent.append(kw) or True)
    return sent


@pytest.fixture
def no_whatsapp_send(monkeypatch):
    class _Silent:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, *a, **kw):
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Silent)


@pytest.fixture
def llm():
    holder = {}

    def use(responses):
        fake = FakeLLMClient(responses)
        app.dependency_overrides[get_llm_client] = lambda: fake
        holder["fake"] = fake
        return fake

    yield use
    app.dependency_overrides.pop(get_llm_client, None)


async def _conversation_with_old_transfer(db_session, tenant, number: str, *, human_reply: str | None = None) -> Conversation:
    """Reproduit l'état d'aujourd'hui : demande de négociation transmise, puis conversation rendue à Bob."""
    customer = Customer(tenant_id=tenant.id, whatsapp_number=number)
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.flush()
    past = datetime.now(timezone.utc) - timedelta(hours=8)
    rows = [
        (MessageSender.CUSTOMER, "text", "Je voudrais négocier le prix du Samsung A56"),
        (MessageSender.SYSTEM, "handoff", "Transfert vers un humain : Demande de négociation de prix pour un Samsung A56."),
        (MessageSender.AI, "text", "Je transmets votre demande à un conseiller."),
    ]
    if human_reply:
        rows.append((MessageSender.HUMAN, "text", human_reply))
    rows.append((MessageSender.SYSTEM, "release", "Conversation rendue à l'IA"))
    for i, (sender, kind, content) in enumerate(rows):
        db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=sender,
                               message_type=kind, content=content, created_at=past + timedelta(minutes=i)))
    await db_session.commit()
    return conversation


def _all_text_sent_to_llm(fake: FakeLLMClient) -> str:
    return repr(fake.received_messages)


# --- Webhook : l'incident d'aujourd'hui -----------------------------------------------

@pytest.mark.asyncio
async def test_released_conversation_does_not_resend_old_request_to_llm(
    client, db_session, unique_email, llm, outbox, no_whatsapp_send
):
    tenant, _ = await _setup(db_session, unique_email, "pn-handoff-1")
    conversation = await _conversation_with_old_transfer(db_session, tenant, "33700000001")
    fake = llm([text_response("Bonjour ! Comment puis-je vous aider ?")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-handoff-1", "33700000001", "Bonjour"))

    sent_to_llm = _all_text_sent_to_llm(fake)
    assert "négocier le prix du Samsung" not in sent_to_llm  # la demande déjà transmise n'est plus relue
    assert "Bonjour" in sent_to_llm
    refreshed = (await db_session.execute(select(Conversation).where(Conversation.id == conversation.id)
                                          .execution_options(populate_existing=True))).scalar_one()
    assert refreshed.status == ConversationStatus.ACTIVE
    assert outbox == []


@pytest.mark.asyncio
async def test_llm_sees_what_the_human_advisor_replied(client, db_session, unique_email, llm, outbox, no_whatsapp_send):
    tenant, _ = await _setup(db_session, unique_email, "pn-handoff-2")
    await _conversation_with_old_transfer(db_session, tenant, "33700000002", human_reply="Je vous fais 5 % de remise")
    fake = llm([text_response("Parfait !")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-handoff-2", "33700000002", "D'accord, je prends"))

    assert "Je vous fais 5 % de remise" in _all_text_sent_to_llm(fake)


@pytest.mark.asyncio
async def test_prompt_requires_transfer_to_match_last_message(client, db_session, unique_email, llm, outbox, no_whatsapp_send):
    await _setup(db_session, unique_email, "pn-handoff-3")
    fake = llm([text_response("Bonjour !")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-handoff-3", "33700000003", "Bonjour"))

    assert "DERNIER message du client" in fake.received_systems[0]


# --- Alertes email au commerçant -----------------------------------------------------

async def _conversation_of(db_session, number: str) -> Conversation:
    customer = (await db_session.execute(select(Customer).where(Customer.whatsapp_number == number))).scalar_one()
    return (await db_session.execute(select(Conversation).where(Conversation.customer_id == customer.id)
                                     .execution_options(populate_existing=True))).scalar_one()


@pytest.mark.asyncio
async def test_handoff_by_bob_alerts_the_merchant(client, db_session, unique_email, llm, outbox, no_whatsapp_send):
    await _setup(db_session, unique_email, "pn-handoff-4")
    llm([
        tool_use_response("handoff_to_human", {"reason": "Client mécontent de sa livraison"}),
        text_response("Je transmets à un conseiller."),
    ])

    await client.post("/webhooks/whatsapp", json=_payload("pn-handoff-4", "33700000004", "Ma commande n'est jamais arrivée !"))

    assert len(outbox) == 1
    email = outbox[0]
    assert email["to"] == unique_email
    assert "+33700000004" in email["subject"]
    assert "Raison : Client mécontent de sa livraison" in email["body"]
    conversation = await _conversation_of(db_session, "33700000004")
    assert f"/dashboard/?conversation={conversation.id}" in email["body"]
    assert conversation.status == ConversationStatus.WAITING_HUMAN
    assert conversation.human_alert_sent_at is not None


@pytest.mark.asyncio
async def test_negotiation_escalation_alerts_the_merchant(client, db_session, unique_email, llm, outbox, no_whatsapp_send):
    tenant, product = await _setup(db_session, unique_email, "pn-handoff-5")
    db_session.add(TenantNegotiationSettings(tenant_id=tenant.id, enabled=True, max_discount_pct=10, max_rounds=0))
    await db_session.commit()
    llm([
        tool_use_response("negotiate_price", {"product_id": str(product.id), "customer_offer": 100000}),
        text_response("Un conseiller va reprendre la conversation."),
    ])

    await client.post("/webhooks/whatsapp", json=_payload("pn-handoff-5", "33700000005", "Je vous le prends à 100 000"))

    assert len(outbox) == 1
    assert "Négociation transférée à un humain" in outbox[0]["body"]
    assert (await _conversation_of(db_session, "33700000005")).status == ConversationStatus.WAITING_HUMAN


@pytest.mark.asyncio
async def test_normal_reply_sends_no_alert(client, db_session, unique_email, llm, outbox, no_whatsapp_send):
    await _setup(db_session, unique_email, "pn-handoff-6")
    llm([text_response("Bonjour !")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-handoff-6", "33700000006", "Bonjour"))

    assert outbox == []


async def _waiting_conversation(db_session, tenant, number: str, *, alert_at=None, agent=None) -> Conversation:
    customer = Customer(tenant_id=tenant.id, whatsapp_number=number)
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.WAITING_HUMAN,
                                assigned_agent=agent, human_alert_sent_at=alert_at)
    db_session.add(conversation)
    await db_session.commit()
    return conversation


@pytest.mark.asyncio
async def test_customer_follow_up_triggers_reminder(client, db_session, unique_email, llm, outbox):
    tenant, _ = await _setup(db_session, unique_email, "pn-handoff-7")
    await _waiting_conversation(db_session, tenant, "33700000007", alert_at=datetime.now(timezone.utc) - timedelta(hours=2))
    fake = llm([])

    await client.post("/webhooks/whatsapp", json=_payload("pn-handoff-7", "33700000007", "Allô ? Toujours personne ?"))

    assert len(outbox) == 1
    assert outbox[0]["subject"].startswith("Relance")
    assert "Allô ? Toujours personne ?" in outbox[0]["body"]
    assert fake.call_count == 0  # Bob reste silencieux pendant l'attente


@pytest.mark.asyncio
async def test_reminder_at_most_once_per_hour(client, db_session, unique_email, llm, outbox):
    tenant, _ = await _setup(db_session, unique_email, "pn-handoff-8")
    await _waiting_conversation(db_session, tenant, "33700000008", alert_at=datetime.now(timezone.utc) - timedelta(minutes=20))
    llm([])

    for text in ("Allô ?", "Vous êtes là ?", "???"):
        await client.post("/webhooks/whatsapp", json=_payload("pn-handoff-8", "33700000008", text))

    assert outbox == []


@pytest.mark.asyncio
async def test_no_reminder_when_merchant_took_over_manually(client, db_session, unique_email, llm, outbox):
    tenant, _ = await _setup(db_session, unique_email, "pn-handoff-9")
    owner = (await db_session.execute(select(User).where(User.email == unique_email))).scalar_one()
    await _waiting_conversation(db_session, tenant, "33700000009", agent=owner.id)
    llm([])

    await client.post("/webhooks/whatsapp", json=_payload("pn-handoff-9", "33700000009", "Merci pour votre réponse"))

    assert outbox == []


@pytest.mark.asyncio
async def test_manual_takeover_sends_no_alert(client, db_session, unique_email, outbox):
    tenant, _ = await _setup(db_session, unique_email, "pn-handoff-10")
    customer = Customer(tenant_id=tenant.id, whatsapp_number="33700000010")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    token = (await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})).json()["access_token"]

    r = await client.post(f"/api/v1/conversations/{conversation.id}/takeover", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200
    assert outbox == []


@pytest.mark.asyncio
async def test_alert_goes_only_to_the_right_merchant(client, db_session, llm, outbox, no_whatsapp_send):
    await _setup(db_session, "a@handoff-iso.sn", "pn-handoff-a")
    await _setup(db_session, "b@handoff-iso.sn", "pn-handoff-b")
    llm([tool_use_response("handoff_to_human", {"reason": "Remboursement"}), text_response("Je transmets.")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-handoff-b", "33700000011", "Je veux être remboursé"))

    assert [e["to"] for e in outbox] == ["b@handoff-iso.sn"]


@pytest.mark.asyncio
async def test_email_failure_never_blocks_the_customer_reply(client, db_session, unique_email, llm, monkeypatch):
    await _setup(db_session, unique_email, "pn-handoff-12")
    sent_to_customer: list[str] = []

    class _Recorder:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, to, body):
            sent_to_customer.append(body)
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Recorder)
    monkeypatch.setattr("app.api.webhooks.whatsapp.send_email", lambda **kw: False)  # SMTP en panne
    llm([tool_use_response("handoff_to_human", {"reason": "Litige"}), text_response("Je transmets à un conseiller.")])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-handoff-12", "33700000012", "Litige"))

    assert r.status_code == 200
    assert any("conseiller" in body for body in sent_to_customer)


# --- Unitaires : rappels et affichage ------------------------------------------------

def test_reminder_due_rules():
    conv = Conversation(status=ConversationStatus.WAITING_HUMAN, assigned_agent=None, human_alert_sent_at=None)
    assert reminder_due(conv) is True
    conv.human_alert_sent_at = T0
    assert reminder_due(conv, now=T0 + timedelta(minutes=59)) is False
    assert reminder_due(conv, now=T0 + timedelta(hours=1)) is True
    conv.status = ConversationStatus.ACTIVE
    assert reminder_due(conv, now=T0 + timedelta(hours=5)) is False
    conv.status = ConversationStatus.WAITING_HUMAN
    conv.assigned_agent = uuid.uuid4()
    assert reminder_due(conv, now=T0 + timedelta(hours=5)) is False


def test_customer_display_name():
    assert customer_display_name(Customer(whatsapp_number="221700000000", first_name="Awa", last_name="Diop")) == "Awa Diop (+221700000000)"
    assert customer_display_name(Customer(whatsapp_number="221700000000")) == "+221700000000"


def test_handoff_alert_without_reason():
    conv = Conversation(id=uuid.uuid4())
    subject, body = build_handoff_alert(Customer(whatsapp_number="221700000000"), conv, None)
    assert "Raison : Non précisée" in body
