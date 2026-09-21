import pytest

from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.messaging_settings import KillSwitch, OutboundMode, TenantMessagingSettings
from app.models.tenant import Tenant
from app.models.user import Role, User


async def _setup_tenant_with_conversation(db_session, email: str, role: Role = Role.AGENT):
    tenant = Tenant(name=f"Tenant {email}", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()

    user = User(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password("secret123456"),
        full_name="User",
        role=role,
    )
    db_session.add(user)

    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000099")
    db_session.add(customer)
    await db_session.flush()

    conversation = Conversation(
        tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE
    )
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(conversation)
    return tenant, user, conversation


async def _login(client, email: str, password: str = "secret123456") -> str:
    response = await client.post("/api/v1/auth/login", data={"username": email, "password": password})
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_reply_denied_when_mode_is_ai_only_and_requester_is_human(client, db_session, unique_email):
    tenant, user, conversation = await _setup_tenant_with_conversation(db_session, unique_email, role=Role.MANAGER)

    db_session.add(
        TenantMessagingSettings(tenant_id=tenant.id, outbound_mode=OutboundMode.AI_ONLY, kill_switch=KillSwitch.ALLOWED)
    )
    await db_session.commit()

    token = await _login(client, unique_email)
    response = await client.post(
        "/api/v1/messages/send",
        json={"conversation_id": str(conversation.id), "body": "Bonjour", "is_proactive": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_reply_allowed_in_ai_plus_human_mode(client, db_session, unique_email):
    tenant, user, conversation = await _setup_tenant_with_conversation(db_session, unique_email, role=Role.MANAGER)

    db_session.add(
        TenantMessagingSettings(
            tenant_id=tenant.id, outbound_mode=OutboundMode.AI_PLUS_HUMAN, kill_switch=KillSwitch.ALLOWED
        )
    )
    await db_session.commit()

    token = await _login(client, unique_email)
    response = await client.post(
        "/api/v1/messages/send",
        json={"conversation_id": str(conversation.id), "body": "Bonjour", "is_proactive": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "sent"


@pytest.mark.asyncio
async def test_proactive_message_denied_without_commercial_mode(client, db_session, unique_email):
    tenant, user, conversation = await _setup_tenant_with_conversation(db_session, unique_email, role=Role.MANAGER)

    db_session.add(
        TenantMessagingSettings(
            tenant_id=tenant.id, outbound_mode=OutboundMode.AI_PLUS_HUMAN, kill_switch=KillSwitch.ALLOWED
        )
    )
    await db_session.commit()

    token = await _login(client, unique_email)
    response = await client.post(
        "/api/v1/messages/send",
        json={"conversation_id": str(conversation.id), "body": "Promo !", "is_proactive": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_proactive_message_denied_for_agent_role_even_if_commercial_enabled(client, db_session, unique_email):
    """CAN_SEND_PROACTIVE_MESSAGE réservé aux rôles MANAGER+ dans ce sprint (section 56.4)."""
    tenant, user, conversation = await _setup_tenant_with_conversation(db_session, unique_email, role=Role.AGENT)

    db_session.add(
        TenantMessagingSettings(
            tenant_id=tenant.id, outbound_mode=OutboundMode.COMMERCIAL_ENABLED, kill_switch=KillSwitch.ALLOWED
        )
    )
    await db_session.commit()

    token = await _login(client, unique_email)
    response = await client.post(
        "/api/v1/messages/send",
        json={"conversation_id": str(conversation.id), "body": "Promo !", "is_proactive": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_kill_switch_blocks_everything_even_ai_plus_human(client, db_session, unique_email):
    tenant, user, conversation = await _setup_tenant_with_conversation(db_session, unique_email, role=Role.MANAGER)

    db_session.add(
        TenantMessagingSettings(
            tenant_id=tenant.id, outbound_mode=OutboundMode.AI_PLUS_HUMAN, kill_switch=KillSwitch.BLOCKED
        )
    )
    await db_session.commit()

    token = await _login(client, unique_email)
    response = await client.post(
        "/api/v1/messages/send",
        json={"conversation_id": str(conversation.id), "body": "Bonjour", "is_proactive": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_daily_quota_enforced_for_proactive_messages(client, db_session, unique_email):
    tenant, user, conversation = await _setup_tenant_with_conversation(db_session, unique_email, role=Role.MANAGER)

    db_session.add(
        TenantMessagingSettings(
            tenant_id=tenant.id,
            outbound_mode=OutboundMode.COMMERCIAL_ENABLED,
            kill_switch=KillSwitch.ALLOWED,
            daily_outbound_limit=2,
        )
    )
    await db_session.commit()

    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    for _ in range(2):
        response = await client.post(
            "/api/v1/messages/send",
            json={"conversation_id": str(conversation.id), "body": "Promo !", "is_proactive": True},
            headers=headers,
        )
        assert response.status_code == 200

    third = await client.post(
        "/api/v1/messages/send",
        json={"conversation_id": str(conversation.id), "body": "Promo !", "is_proactive": True},
        headers=headers,
    )
    assert third.status_code == 429


@pytest.mark.asyncio
async def test_no_settings_denies_by_default(client, db_session, unique_email):
    """Section 56.2 — sans configuration explicite, comportement le plus restrictif (refus)."""
    tenant, user, conversation = await _setup_tenant_with_conversation(db_session, unique_email, role=Role.MANAGER)
    # Volontairement : aucune ligne TenantMessagingSettings créée pour ce tenant.

    token = await _login(client, unique_email)
    response = await client.post(
        "/api/v1/messages/send",
        json={"conversation_id": str(conversation.id), "body": "Bonjour", "is_proactive": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_cannot_send_to_another_tenants_conversation(client, db_session, unique_email):
    """Isolation multi-tenant (section 30) appliquée aussi à l'envoi de messages."""
    tenant_a, user_a, conversation_a = await _setup_tenant_with_conversation(
        db_session, unique_email, role=Role.MANAGER
    )
    tenant_b, user_b, conversation_b = await _setup_tenant_with_conversation(
        db_session, f"b_{unique_email}", role=Role.MANAGER
    )

    db_session.add(
        TenantMessagingSettings(
            tenant_id=tenant_a.id, outbound_mode=OutboundMode.AI_PLUS_HUMAN, kill_switch=KillSwitch.ALLOWED
        )
    )
    await db_session.commit()

    token_a = await _login(client, unique_email)
    response = await client.post(
        "/api/v1/messages/send",
        # user A essaie d'envoyer sur la conversation du tenant B
        json={"conversation_id": str(conversation_b.id), "body": "Intrusion", "is_proactive": False},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert response.status_code == 404
