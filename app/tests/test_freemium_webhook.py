import pytest

from app.agents.dependency import get_llm_client
from app.core.security import hash_password
from app.main import app
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.tests.fakes import FakeLLMClient, text_response


async def _setup(db_session, email: str, phone_number_id: str, is_paid: bool = False):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, is_paid=is_paid)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=phone_number_id, system_user_token="t"))
    await db_session.commit()
    await db_session.refresh(tenant)
    return tenant


def _payload(phone_number_id, from_number, text):
    return {
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": phone_number_id},
            "messages": [{"from": from_number, "id": f"wamid.{from_number}", "type": "text", "text": {"body": text}, "timestamp": "1"}],
        }}]}]
    }


@pytest.mark.asyncio
async def test_freemium_reply_includes_bob_mention(client, db_session, unique_email):
    await _setup(db_session, unique_email, "phone_freemium_test", is_paid=False)
    fake = FakeLLMClient([text_response("Voici notre produit.")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        response = await client.post("/webhooks/whatsapp", json=_payload("phone_freemium_test", "221700000001", "Bonjour"))
        assert "Propulsé par Bob" in response.json()["ai_reply"]
    finally:
        del app.dependency_overrides[get_llm_client]


@pytest.mark.asyncio
async def test_paid_reply_never_includes_bob_mention(client, db_session, unique_email):
    await _setup(db_session, unique_email, "phone_paid_test", is_paid=True)
    fake = FakeLLMClient([text_response("Voici notre produit.")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        response = await client.post("/webhooks/whatsapp", json=_payload("phone_paid_test", "221700000002", "Bonjour"))
        assert "Propulsé par Bob" not in response.json()["ai_reply"]
    finally:
        del app.dependency_overrides[get_llm_client]


@pytest.mark.asyncio
async def test_freemium_quota_exceeded_returns_fallback_without_calling_llm(client, db_session, unique_email):
    from app.services.plan_limits import FREEMIUM_MAX_CONVERSATIONS_PER_MONTH

    tenant = await _setup(db_session, unique_email, "phone_quota_test", is_paid=False)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000003")
    db_session.add(customer)
    await db_session.flush()
    for _ in range(FREEMIUM_MAX_CONVERSATIONS_PER_MONTH + 1):
        db_session.add(Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.CLOSED))
    await db_session.commit()

    fake = FakeLLMClient([text_response("Ne devrait jamais être appelé")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        response = await client.post("/webhooks/whatsapp", json=_payload("phone_quota_test", "221700000003", "Bonjour"))
        reply = response.json()["ai_reply"]
        assert fake.call_count == 0  # le LLM n'a jamais été appelé
        assert "Boutique" in reply  # message de repli mentionne l'entreprise
    finally:
        del app.dependency_overrides[get_llm_client]
