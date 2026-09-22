import pytest

from app.agents.dependency import get_llm_client
from app.core.security import hash_password
from app.main import app
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.tests.fakes import FakeLLMClient, text_response


async def _setup(db_session, email: str, role: Role = Role.AGENT):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=TenantPlan.INDEPENDANT)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role)
    )
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(conversation)
    return tenant, customer, conversation


async def _login(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_takeover_sets_waiting_human_and_assigns_agent(client, db_session, unique_email):
    tenant, customer, conversation = await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    response = await client.post(
        f"/api/v1/conversations/{conversation.id}/takeover", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "WAITING_HUMAN"
    assert body["assigned_agent"] is not None


@pytest.mark.asyncio
async def test_release_returns_conversation_to_ai(client, db_session, unique_email):
    tenant, customer, conversation = await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    await client.post(f"/api/v1/conversations/{conversation.id}/takeover", headers={"Authorization": f"Bearer {token}"})
    response = await client.post(
        f"/api/v1/conversations/{conversation.id}/release", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ACTIVE"
    assert body["assigned_agent"] is None


@pytest.mark.asyncio
async def test_takeover_and_release_log_system_messages(client, db_session, unique_email):
    tenant, customer, conversation = await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    await client.post(f"/api/v1/conversations/{conversation.id}/takeover", headers=headers)
    await client.post(f"/api/v1/conversations/{conversation.id}/release", headers=headers)

    detail = await client.get(f"/api/v1/conversations/{conversation.id}", headers=headers)
    contents = [m["content"] for m in detail.json()["messages"]]
    assert any("pris le contrôle" in c for c in contents)
    assert any("rendue à l'IA" in c for c in contents)


@pytest.mark.asyncio
async def test_conversations_isolated_by_tenant(client, db_session, unique_email):
    tenant_a, customer_a, conversation_a = await _setup(db_session, unique_email)
    tenant_b, customer_b, conversation_b = await _setup(db_session, f"b_{unique_email}")

    token_b = await _login(client, f"b_{unique_email}")
    response = await client.post(
        f"/api/v1/conversations/{conversation_a.id}/takeover", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_ai_stops_after_takeover_then_resumes_after_release(client, db_session, unique_email):
    """Bout en bout : takeover -> l'IA se tait ; release -> l'IA répond de nouveau (section 28)."""
    tenant, customer, conversation = await _setup(db_session, unique_email, role=Role.MANAGER)
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="phone_takeover_test", system_user_token="t"))
    await db_session.commit()

    token = await _login(client, unique_email)

    def payload(text, msg_id):
        return {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "phone_takeover_test"},
                                "messages": [
                                    {"from": "221700000000", "id": msg_id, "type": "text", "text": {"body": text}, "timestamp": "1"}
                                ],
                            }
                        }
                    ]
                }
            ]
        }

    fake = FakeLLMClient([text_response("Réponse IA")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        await client.post("/api/v1/conversations/" + str(conversation.id) + "/takeover", headers={"Authorization": f"Bearer {token}"})

        r1 = await client.post("/webhooks/whatsapp", json=payload("Bonjour", "wamid.1"))
        assert "ai_reply" not in r1.json()  # l'IA ne répond pas, conversation prise en main

        await client.post("/api/v1/conversations/" + str(conversation.id) + "/release", headers={"Authorization": f"Bearer {token}"})

        r2 = await client.post("/webhooks/whatsapp", json=payload("Toujours là ?", "wamid.2"))
        assert r2.json().get("ai_reply") == "Réponse IA"  # l'IA répond de nouveau
    finally:
        del app.dependency_overrides[get_llm_client]
