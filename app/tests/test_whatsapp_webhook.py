import pytest

from app.core.security import hash_password
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount


async def _setup_tenant_with_whatsapp(db_session, email: str, phone_number_id: str) -> Tenant:
    tenant = Tenant(name=f"Tenant {email}", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()

    owner = User(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password("secret123456"),
        full_name="Owner",
        role=Role.OWNER,
    )
    db_session.add(owner)

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        waba_id="waba_test",
        phone_number_id=phone_number_id,
        system_user_token="fake-token",
    )
    db_session.add(account)
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
                                {
                                    "from": from_number,
                                    "id": "wamid.TEST123",
                                    "type": "text",
                                    "text": {"body": text},
                                    "timestamp": "1700000000",
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


@pytest.mark.asyncio
async def test_webhook_verification_success(client):
    from app.core.config import get_settings

    settings = get_settings()
    settings.whatsapp_webhook_verify_token = "test-verify-token"

    response = await client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "test-verify-token", "hub.challenge": "12345"},
    )
    assert response.status_code == 200
    assert response.json() == 12345


@pytest.mark.asyncio
async def test_webhook_verification_wrong_token_rejected(client):
    response = await client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong-token", "hub.challenge": "12345"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_incoming_message_routed_to_correct_tenant(client, db_session):
    tenant_a = await _setup_tenant_with_whatsapp(db_session, "a@example.com", "phone_number_id_A")
    tenant_b = await _setup_tenant_with_whatsapp(db_session, "b@example.com", "phone_number_id_B")

    payload = _incoming_message_payload("phone_number_id_A", "221700000001", "Bonjour, avez-vous un iPhone 15 ?")
    response = await client.post("/webhooks/whatsapp", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "received"

    from sqlalchemy import select

    from app.models.conversation import Message

    result = await db_session.execute(select(Message))
    messages = result.scalars().all()

    assert len(messages) == 1
    assert messages[0].tenant_id == tenant_a.id
    assert messages[0].tenant_id != tenant_b.id
    assert "iPhone 15" in messages[0].content


@pytest.mark.asyncio
async def test_webhook_unknown_phone_number_id_does_not_fail(client, db_session):
    payload = _incoming_message_payload("phone_number_id_INCONNU", "221700000001", "Test")
    response = await client.post("/webhooks/whatsapp", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "unknown_phone_number_id"


@pytest.mark.asyncio
async def test_webhook_status_update_ignored(client, db_session):
    """Un accusé de statut (delivered/read) ne doit jamais faire échouer le webhook (section 7)."""
    status_payload = {
        "entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "x"}, "statuses": [{"status": "delivered"}]}}]}]
    }
    response = await client.post("/webhooks/whatsapp", json=status_payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
