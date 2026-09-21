from datetime import datetime, timedelta, timezone

import pytest

from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount


@pytest.mark.asyncio
async def test_incoming_message_resets_followup_stage(client, db_session, unique_email):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=unique_email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="phone_reset_test", system_user_token="t"))
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000099")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(
        tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
        last_message_at=datetime.now(timezone.utc) - timedelta(hours=100),
        followup_stage=1,
        last_followup_at=datetime.now(timezone.utc) - timedelta(hours=50),
    )
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(conversation)

    payload = {
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "phone_reset_test"},
            "messages": [{"from": "221700000099", "id": "wamid.X", "type": "text", "text": {"body": "Bonjour, je suis toujours là"}, "timestamp": "1"}],
        }}]}]
    }
    response = await client.post("/webhooks/whatsapp", json=payload)
    assert response.status_code == 200

    await db_session.refresh(conversation)
    assert conversation.followup_stage == 0
    assert conversation.last_followup_at is None
