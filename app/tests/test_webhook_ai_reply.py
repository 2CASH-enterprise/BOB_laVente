import pytest

from app.agents.dependency import get_llm_client
from app.core.security import hash_password
from app.main import app
from app.models.conversation import ConversationStatus
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response


async def _setup_tenant_with_whatsapp_and_product(db_session, email: str, phone_number_id: str):
    tenant = Tenant(name="Boutique Test", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()

    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    db_session.add(
        WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=phone_number_id, system_user_token="t")
    )
    db_session.add(Product(tenant_id=tenant.id, sku="SAM-A56", name="Samsung A56", price=280000, currency="XOF", stock_quantity=12))
    await db_session.commit()
    return tenant


def _incoming_message_payload(phone_number_id: str, from_number: str, text: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "messages": [
                                {"from": from_number, "id": "wamid.X", "type": "text", "text": {"body": text}, "timestamp": "1700000000"}
                            ],
                        }
                    }
                ]
            }
        ]
    }


@pytest.mark.asyncio
async def test_webhook_generates_and_persists_ai_reply(client, db_session, unique_email):
    await _setup_tenant_with_whatsapp_and_product(db_session, unique_email, "phone_ai_test")

    fake = FakeLLMClient(
        [
            tool_use_response("search_products", {"query": "Samsung"}),
            text_response("Nous avons le Samsung A56 à 280 000 XOF, disponible."),
        ]
    )
    app.dependency_overrides[get_llm_client] = lambda: fake

    try:
        payload = _incoming_message_payload("phone_ai_test", "221700000001", "Avez-vous un Samsung ?")
        response = await client.post("/webhooks/whatsapp", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "received"
        assert "280 000" in body["ai_reply"]
    finally:
        del app.dependency_overrides[get_llm_client]

    from sqlalchemy import select

    from app.models.conversation import Message, MessageSender

    result = await db_session.execute(select(Message).order_by(Message.created_at))
    messages = result.scalars().all()
    assert len(messages) == 2
    assert messages[0].sender == MessageSender.CUSTOMER
    assert messages[1].sender == MessageSender.AI
    assert "280 000" in messages[1].content


@pytest.mark.asyncio
async def test_webhook_without_llm_configured_persists_message_only(client, db_session, unique_email):
    """Sans ANTHROPIC_API_KEY configurée (get_llm_client -> None), pas de plantage, pas de réponse auto."""
    await _setup_tenant_with_whatsapp_and_product(db_session, unique_email, "phone_no_llm")

    app.dependency_overrides[get_llm_client] = lambda: None
    try:
        payload = _incoming_message_payload("phone_no_llm", "221700000002", "Bonjour")
        response = await client.post("/webhooks/whatsapp", json=payload)
        assert response.status_code == 200
        assert response.json()["status"] == "received"
        assert "ai_reply" not in response.json()
    finally:
        del app.dependency_overrides[get_llm_client]


@pytest.mark.asyncio
async def test_webhook_sends_reply_via_whatsapp_client(client, db_session, unique_email, monkeypatch):
    """Vérifie que la réponse générée est réellement transmise à l'API WhatsApp (pas juste stockée)."""
    tenant = await _setup_tenant_with_whatsapp_and_product(db_session, unique_email, "phone_send_test")

    fake = FakeLLMClient([text_response("Réponse à envoyer")])
    app.dependency_overrides[get_llm_client] = lambda: fake

    sent_calls = []

    class FakeWhatsAppClient:
        def __init__(self, phone_number_id, system_user_token):
            self.phone_number_id = phone_number_id
            self.system_user_token = system_user_token

        async def send_text_message(self, to, body):
            sent_calls.append({"to": to, "body": body})
            return {"messages": [{"id": "wamid.SENT"}]}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", FakeWhatsAppClient)

    try:
        payload = _incoming_message_payload("phone_send_test", "221700000009", "Bonjour")
        response = await client.post("/webhooks/whatsapp", json=payload)
        assert response.status_code == 200
    finally:
        del app.dependency_overrides[get_llm_client]

    assert len(sent_calls) == 1
    assert sent_calls[0]["to"] == "221700000009"
    assert sent_calls[0]["body"] == "Réponse à envoyer"


@pytest.mark.asyncio
async def test_webhook_whatsapp_send_failure_does_not_break_response(client, db_session, unique_email, monkeypatch):
    """Section 34 — une panne de l'API WhatsApp ne doit jamais faire échouer le webhook."""
    tenant = await _setup_tenant_with_whatsapp_and_product(db_session, unique_email, "phone_send_fail_test")

    fake = FakeLLMClient([text_response("Réponse")])
    app.dependency_overrides[get_llm_client] = lambda: fake

    class BrokenWhatsAppClient:
        def __init__(self, phone_number_id, system_user_token):
            pass

        async def send_text_message(self, to, body):
            raise RuntimeError("Token WhatsApp expiré")

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", BrokenWhatsAppClient)

    try:
        payload = _incoming_message_payload("phone_send_fail_test", "221700000010", "Bonjour")
        response = await client.post("/webhooks/whatsapp", json=payload)
        assert response.status_code == 200
        assert response.json()["ai_reply"] == "Réponse"
    finally:
        del app.dependency_overrides[get_llm_client]


@pytest.mark.asyncio
async def test_webhook_no_ai_reply_when_conversation_waiting_human(client, db_session, unique_email):
    """Section 28 — une fois qu'un humain a pris la main, l'IA ne doit plus répondre automatiquement."""
    tenant = await _setup_tenant_with_whatsapp_and_product(db_session, unique_email, "phone_human_takeover")

    from app.models.conversation import Conversation
    from app.models.customer import Customer

    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000003")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.WAITING_HUMAN)
    db_session.add(conversation)
    await db_session.commit()

    fake = FakeLLMClient([text_response("Ne devrait jamais être appelé")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        payload = _incoming_message_payload("phone_human_takeover", "221700000003", "Bonjour")
        response = await client.post("/webhooks/whatsapp", json=payload)
        assert response.status_code == 200
        assert "ai_reply" not in response.json()
        assert fake.call_count == 0
    finally:
        del app.dependency_overrides[get_llm_client]
