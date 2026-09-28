"""
Garde-fou des envois sortants (section 56) et réponse d'un humain depuis le dashboard (lot 23b).

Depuis le lot 23b, le message part réellement sur WhatsApp : les tests remplacent le client
WhatsApp par un faux qui enregistre les envois.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.security import hash_password
from app.integrations.whatsapp.client import WhatsAppSendError
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.messaging_settings import (
    KillSwitch,
    MessageSendAudit,
    OutboundMode,
    SendResult,
    TenantMessagingSettings,
)
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.messaging_guard import OutboundDenied, Permission, check_and_log_outbound


class _FakeWhatsApp:
    sent: list = []
    fail_with: Exception | None = None

    def __init__(self, phone_number_id, system_user_token):
        self.phone_number_id = phone_number_id

    async def send_text_message(self, to, body):
        if _FakeWhatsApp.fail_with is not None:
            raise _FakeWhatsApp.fail_with
        _FakeWhatsApp.sent.append({"to": to, "body": body, "phone_number_id": self.phone_number_id})
        return {"messages": [{"id": "wamid.TEST"}]}


@pytest.fixture(autouse=True)
def fake_whatsapp(monkeypatch):
    _FakeWhatsApp.sent = []
    _FakeWhatsApp.fail_with = None
    monkeypatch.setattr("app.api.messages.routes.WhatsAppClient", _FakeWhatsApp)
    return _FakeWhatsApp


async def _setup_tenant_with_conversation(
    db_session,
    email: str,
    role: Role = Role.AGENT,
    status: ConversationStatus = ConversationStatus.WAITING_HUMAN,
    last_customer_message_age: timedelta | None = timedelta(hours=1),
    with_account: bool = True,
):
    tenant = Tenant(name=f"Tenant {email}", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()

    db_session.add(User(
        tenant_id=tenant.id, email=email, hashed_password=hash_password("secret123456"), full_name="User", role=role,
    ))
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000099")
    db_session.add(customer)
    await db_session.flush()

    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=status)
    db_session.add(conversation)
    await db_session.flush()
    if last_customer_message_age is not None:
        db_session.add(Message(
            tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
            content="Bonjour", created_at=datetime.now(timezone.utc) - last_customer_message_age,
        ))
    if with_account:
        db_session.add(WhatsAppAccount(
            tenant_id=tenant.id, waba_id="w", phone_number_id=f"pn_{uuid.uuid4().hex[:10]}", system_user_token="t",
        ))
    await db_session.commit()
    await db_session.refresh(conversation)
    return tenant, conversation


async def _settings(db_session, tenant, mode, kill=KillSwitch.ALLOWED, limit=None):
    row = TenantMessagingSettings(tenant_id=tenant.id, outbound_mode=mode, kill_switch=kill)
    if limit is not None:
        row.daily_outbound_limit = limit
    db_session.add(row)
    await db_session.commit()


async def _login(client, email: str, password: str = "secret123456") -> dict:
    response = await client.post("/api/v1/auth/login", data={"username": email, "password": password})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _send(client, headers, conversation, body="Bonjour", proactive=False):
    return await client.post(
        "/api/v1/messages/send",
        json={"conversation_id": str(conversation.id), "body": body, "is_proactive": proactive},
        headers=headers,
    )


async def _human_messages(db_session, conversation):
    from sqlalchemy import select

    rows = (await db_session.execute(select(Message).where(
        Message.conversation_id == conversation.id, Message.sender == MessageSender.HUMAN,
    ))).scalars().all()
    return rows


# --- Réponse d'un humain ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_human_reply_is_really_sent_on_whatsapp_and_stored(client, db_session, unique_email, fake_whatsapp):
    """Mode par défaut (IA uniquement) : un humain qui a pris le contrôle peut répondre."""
    tenant, conversation = await _setup_tenant_with_conversation(db_session, unique_email)
    headers = await _login(client, unique_email)

    response = await _send(client, headers, conversation, body="  Bonjour, ici l'équipe de la boutique.  ")

    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert fake_whatsapp.sent == [{
        "to": "221700000099", "body": "Bonjour, ici l'équipe de la boutique.",
        "phone_number_id": fake_whatsapp.sent[0]["phone_number_id"],
    }]
    stored = await _human_messages(db_session, conversation)
    assert [m.content for m in stored] == ["Bonjour, ici l'équipe de la boutique."]
    assert stored[0].message_metadata["wa_message_id"] == "wamid.TEST"


@pytest.mark.asyncio
async def test_no_human_reply_while_bob_is_answering(client, db_session, unique_email, fake_whatsapp):
    """Jamais Bob et un humain en même temps : il faut prendre le contrôle d'abord."""
    tenant, conversation = await _setup_tenant_with_conversation(
        db_session, unique_email, status=ConversationStatus.ACTIVE,
    )
    await _settings(db_session, tenant, OutboundMode.AI_PLUS_HUMAN)
    headers = await _login(client, unique_email)

    response = await _send(client, headers, conversation)

    assert response.status_code == 409
    assert "Prenez d'abord le contrôle" in response.json()["detail"]
    assert fake_whatsapp.sent == []


@pytest.mark.asyncio
async def test_no_settings_still_blocks_human_outside_a_handoff(client, db_session, unique_email, fake_whatsapp):
    """Section 56.2 conservée : sans configuration, un humain ne répond pas tant que Bob a la main."""
    tenant, conversation = await _setup_tenant_with_conversation(
        db_session, unique_email, role=Role.MANAGER, status=ConversationStatus.ACTIVE,
    )
    headers = await _login(client, unique_email)

    response = await _send(client, headers, conversation)

    assert response.status_code == 409
    assert fake_whatsapp.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [timedelta(hours=20, minutes=1), timedelta(hours=25), None])
async def test_free_text_refused_after_20h_or_if_the_customer_never_wrote(client, db_session, unique_email, fake_whatsapp, age):
    tenant, conversation = await _setup_tenant_with_conversation(db_session, unique_email, last_customer_message_age=age)
    headers = await _login(client, unique_email)

    response = await _send(client, headers, conversation)

    assert response.status_code == 409
    assert "20 h" in response.json()["detail"]
    assert fake_whatsapp.sent == []
    assert await _human_messages(db_session, conversation) == []


@pytest.mark.asyncio
async def test_meta_refusal_is_reported_and_nothing_is_stored(client, db_session, unique_email, fake_whatsapp):
    """Jamais « envoyé » pour un message qui n'est pas parti."""
    tenant, conversation = await _setup_tenant_with_conversation(db_session, unique_email)
    fake_whatsapp.fail_with = WhatsAppSendError(400, "code 131047 : Re-engagement message")
    headers = await _login(client, unique_email)

    response = await _send(client, headers, conversation)

    assert response.status_code == 502
    assert "131047" in response.json()["detail"]
    assert await _human_messages(db_session, conversation) == []


@pytest.mark.asyncio
async def test_network_failure_is_reported_without_details(client, db_session, unique_email, fake_whatsapp):
    tenant, conversation = await _setup_tenant_with_conversation(db_session, unique_email)
    fake_whatsapp.fail_with = TimeoutError("jeton-secret dans une trace")
    headers = await _login(client, unique_email)

    response = await _send(client, headers, conversation)

    assert response.status_code == 502
    assert "jeton" not in response.json()["detail"]
    assert await _human_messages(db_session, conversation) == []


@pytest.mark.asyncio
async def test_no_whatsapp_account_is_explained(client, db_session, unique_email, fake_whatsapp):
    tenant, conversation = await _setup_tenant_with_conversation(db_session, unique_email, with_account=False)
    headers = await _login(client, unique_email)

    response = await _send(client, headers, conversation)

    assert response.status_code == 409
    assert "Aucun compte WhatsApp" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", "   ", "x" * 4097])
async def test_empty_or_too_long_message_is_refused(client, db_session, unique_email, fake_whatsapp, body):
    tenant, conversation = await _setup_tenant_with_conversation(db_session, unique_email)
    headers = await _login(client, unique_email)

    response = await _send(client, headers, conversation, body=body)

    assert response.status_code == 422
    assert fake_whatsapp.sent == []


@pytest.mark.asyncio
async def test_refusals_before_the_guard_are_not_counted_as_sent(client, db_session, unique_email, fake_whatsapp):
    from sqlalchemy import func, select

    tenant, conversation = await _setup_tenant_with_conversation(
        db_session, unique_email, last_customer_message_age=timedelta(days=3),
    )
    headers = await _login(client, unique_email)
    await _send(client, headers, conversation)

    count = (await db_session.execute(select(func.count()).where(
        MessageSendAudit.tenant_id == tenant.id, MessageSendAudit.result == SendResult.SENT,
    ))).scalar_one()
    assert count == 0


# --- Garde-fou (section 56) ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_guard_ai_only_blocks_human_unless_in_control(db_session, unique_email):
    tenant, conversation = await _setup_tenant_with_conversation(db_session, unique_email)
    await _settings(db_session, tenant, OutboundMode.AI_ONLY)

    with pytest.raises(OutboundDenied):
        await check_and_log_outbound(db_session, tenant_id=tenant.id, requested_by="user-1",
                                     permission=Permission.CAN_REPLY_TO_CUSTOMER)
    await check_and_log_outbound(db_session, tenant_id=tenant.id, requested_by="user-1",
                                 permission=Permission.CAN_REPLY_TO_CUSTOMER, human_in_control=True)


@pytest.mark.asyncio
async def test_proactive_message_denied_without_commercial_mode(client, db_session, unique_email, fake_whatsapp):
    tenant, conversation = await _setup_tenant_with_conversation(db_session, unique_email, role=Role.MANAGER)
    await _settings(db_session, tenant, OutboundMode.AI_PLUS_HUMAN)
    headers = await _login(client, unique_email)

    response = await _send(client, headers, conversation, body="Promo !", proactive=True)

    assert response.status_code == 403
    assert fake_whatsapp.sent == []


@pytest.mark.asyncio
async def test_proactive_message_denied_for_agent_role_even_if_commercial_enabled(client, db_session, unique_email, fake_whatsapp):
    """CAN_SEND_PROACTIVE_MESSAGE réservé aux rôles MANAGER+ dans ce sprint (section 56.4)."""
    tenant, conversation = await _setup_tenant_with_conversation(db_session, unique_email, role=Role.AGENT)
    await _settings(db_session, tenant, OutboundMode.COMMERCIAL_ENABLED)
    headers = await _login(client, unique_email)

    response = await _send(client, headers, conversation, body="Promo !", proactive=True)

    assert response.status_code == 403
    assert fake_whatsapp.sent == []


@pytest.mark.asyncio
async def test_kill_switch_blocks_everything_even_a_handoff(client, db_session, unique_email, fake_whatsapp):
    tenant, conversation = await _setup_tenant_with_conversation(db_session, unique_email, role=Role.MANAGER)
    await _settings(db_session, tenant, OutboundMode.AI_PLUS_HUMAN, kill=KillSwitch.BLOCKED)
    headers = await _login(client, unique_email)

    response = await _send(client, headers, conversation)

    assert response.status_code == 403
    assert fake_whatsapp.sent == []


@pytest.mark.asyncio
async def test_daily_quota_enforced_for_proactive_messages(client, db_session, unique_email, fake_whatsapp):
    tenant, conversation = await _setup_tenant_with_conversation(
        db_session, unique_email, role=Role.MANAGER, status=ConversationStatus.ACTIVE,
    )
    await _settings(db_session, tenant, OutboundMode.COMMERCIAL_ENABLED, limit=2)
    headers = await _login(client, unique_email)

    for _ in range(2):
        response = await _send(client, headers, conversation, body="Promo !", proactive=True)
        assert response.status_code == 200

    third = await _send(client, headers, conversation, body="Promo !", proactive=True)
    assert third.status_code == 429
    assert len(fake_whatsapp.sent) == 2


@pytest.mark.asyncio
async def test_cannot_send_to_another_tenants_conversation(client, db_session, unique_email, fake_whatsapp):
    """Isolation multi-tenant (section 30) appliquée aussi à l'envoi de messages."""
    tenant_a, conversation_a = await _setup_tenant_with_conversation(db_session, unique_email, role=Role.MANAGER)
    tenant_b, conversation_b = await _setup_tenant_with_conversation(db_session, f"b_{unique_email}", role=Role.MANAGER)
    headers_a = await _login(client, unique_email)

    response = await _send(client, headers_a, conversation_b, body="Intrusion")

    assert response.status_code == 404
    assert fake_whatsapp.sent == []


# --- Fenêtre affichée dans le détail de la conversation -------------------------------------

@pytest.mark.asyncio
async def test_conversation_detail_shows_when_the_reply_window_closes(client, db_session, unique_email):
    tenant, conversation = await _setup_tenant_with_conversation(db_session, unique_email)
    headers = await _login(client, unique_email)

    detail = (await client.get(f"/api/v1/conversations/{conversation.id}", headers=headers)).json()

    closes_at = datetime.fromisoformat(detail["reply_window_closes_at"])
    closes_at = closes_at if closes_at.tzinfo else closes_at.replace(tzinfo=timezone.utc)
    remaining = closes_at - datetime.now(timezone.utc)
    assert timedelta(hours=18) < remaining < timedelta(hours=20)


@pytest.mark.asyncio
async def test_reply_still_possible_just_before_20h(client, db_session, unique_email, fake_whatsapp):
    tenant, conversation = await _setup_tenant_with_conversation(
        db_session, unique_email, last_customer_message_age=timedelta(hours=19, minutes=55),
    )
    headers = await _login(client, unique_email)

    assert (await _send(client, headers, conversation)).status_code == 200


@pytest.mark.asyncio
async def test_only_a_customer_message_opens_the_window(client, db_session, unique_email, fake_whatsapp):
    """Un message récent de Bob, d'un humain ou du système n'ouvre jamais la fenêtre."""
    tenant, conversation = await _setup_tenant_with_conversation(
        db_session, unique_email, last_customer_message_age=timedelta(hours=30),
    )
    for sender in (MessageSender.AI, MessageSender.HUMAN, MessageSender.SYSTEM):
        db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=sender,
                               content="récent", created_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    await db_session.commit()
    headers = await _login(client, unique_email)

    response = await _send(client, headers, conversation)

    assert response.status_code == 409
    assert fake_whatsapp.sent == []
