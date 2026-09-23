import pytest

from app.agents.dependency import get_llm_client
from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.main import app
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.consent_service import is_opt_out_message
from app.tests.fakes import FakeLLMClient, text_response


async def _setup(db_session, email: str, phone_number_id: str = "phone_consent_test"):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=TenantPlan.INDEPENDANT)
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


@pytest.mark.parametrize("text", ["STOP", "stop", "Stop.", "ARRET", "arrêt", "désinscrire", "unsubscribe"])
def test_is_opt_out_message_recognizes_keywords(text):
    assert is_opt_out_message(text) is True


@pytest.mark.parametrize("text", ["stop the car please", "je veux arrêter ma commande", "Bonjour"])
def test_is_opt_out_message_never_false_positive_on_normal_text(text):
    assert is_opt_out_message(text) is False


@pytest.mark.asyncio
async def test_stop_keyword_withdraws_consent_without_calling_llm(client, db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    fake = FakeLLMClient([text_response("Ne devrait jamais être appelé")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        response = await client.post("/webhooks/whatsapp", json=_payload("phone_consent_test", "221700000010", "STOP"))
        assert response.status_code == 200
        assert fake.call_count == 0
        assert "désinscrit" in response.json()["ai_reply"].lower()
    finally:
        del app.dependency_overrides[get_llm_client]

    from sqlalchemy import select

    customer = (await db_session.execute(select(Customer).where(Customer.whatsapp_number == "221700000010"))).scalar_one()
    assert customer.marketing_consent is False
    assert customer.marketing_consent_withdrawn_at is not None


@pytest.mark.asyncio
async def test_stop_withdrawal_recorded_even_without_llm_configured(client, db_session, unique_email):
    """Le retrait doit être enregistré même si aucun LLM n'est configuré (section consentement)."""
    tenant = await _setup(db_session, unique_email)
    response = await client.post("/webhooks/whatsapp", json=_payload("phone_consent_test", "221700000011", "STOP"))
    assert response.status_code == 200

    from sqlalchemy import select

    customer = (await db_session.execute(select(Customer).where(Customer.whatsapp_number == "221700000011"))).scalar_one()
    assert customer.marketing_consent_withdrawn_at is not None


@pytest.mark.asyncio
async def test_stop_never_sent_while_human_has_taken_over(client, db_session, unique_email):
    """Le retrait est enregistré, mais l'IA ne doit jamais répondre pendant une prise de contrôle humaine."""
    tenant = await _setup(db_session, unique_email)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000012")
    db_session.add(customer)
    await db_session.flush()
    db_session.add(Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.WAITING_HUMAN))
    await db_session.commit()

    fake = FakeLLMClient([text_response("Ne devrait jamais être appelé")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        response = await client.post("/webhooks/whatsapp", json=_payload("phone_consent_test", "221700000012", "STOP"))
        assert response.json() == {"status": "received"}
        assert fake.call_count == 0
    finally:
        del app.dependency_overrides[get_llm_client]

    await db_session.refresh(customer)
    assert customer.marketing_consent_withdrawn_at is not None  # toujours enregistré


@pytest.mark.asyncio
async def test_record_marketing_consent_tool_grants_with_source(db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000013")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(customer)

    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("record_marketing_consent", {"accepted": True})
    assert result["status"] == "saved"

    await db_session.refresh(customer)
    assert customer.marketing_consent is True
    assert customer.marketing_consent_source == "AI_ASKED"
    assert customer.marketing_consent_given_at is not None


@pytest.mark.asyncio
async def test_manual_dashboard_toggle_uses_manual_source(client, db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000014")
    db_session.add(customer)
    await db_session.commit()
    await db_session.refresh(customer)

    login = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    token = login.json()["access_token"]

    response = await client.put(
        f"/api/v1/customers/{customer.id}", json={"marketing_consent": True}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.json()["marketing_consent_source"] == "MANUAL"
    assert response.json()["marketing_consent_given_at"] is not None


@pytest.mark.asyncio
async def test_re_granting_consent_clears_withdrawal_timestamp(db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000015")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(customer)

    executor = ToolExecutor(db_session, tenant.id, conversation)
    await executor.execute("record_marketing_consent", {"accepted": False})
    await db_session.refresh(customer)
    assert customer.marketing_consent_withdrawn_at is not None

    await executor.execute("record_marketing_consent", {"accepted": True})
    await db_session.refresh(customer)
    assert customer.marketing_consent is True
    assert customer.marketing_consent_withdrawn_at is None
