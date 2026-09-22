from datetime import datetime, timedelta, timezone

import pytest

from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.followup_settings import TenantFollowupSettings
from app.models.messaging_settings import KillSwitch, OutboundMode, TenantMessagingSettings
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.followup_service import find_eligible_conversations, run_followups_for_tenant


async def _setup(db_session, email: str, followup_enabled=True, outbound_mode=OutboundMode.COMMERCIAL_ENABLED):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, is_paid=True)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    db_session.add(
        WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="p", system_user_token="t")
    )
    db_session.add(
        TenantMessagingSettings(tenant_id=tenant.id, outbound_mode=outbound_mode, kill_switch=KillSwitch.ALLOWED)
    )
    db_session.add(
        TenantFollowupSettings(tenant_id=tenant.id, enabled=followup_enabled, first_followup_hours=24, second_followup_hours=72)
    )
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    await db_session.commit()
    await db_session.refresh(tenant)
    return tenant, customer


def _now():
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_conversation_not_eligible_before_threshold(db_session, unique_email):
    tenant, customer = await _setup(db_session, unique_email)
    conv = Conversation(
        tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
        last_message_at=_now() - timedelta(hours=1),  # trop récent
    )
    db_session.add(conv)
    await db_session.commit()

    from sqlalchemy import select

    settings = (await db_session.execute(select(TenantFollowupSettings).where(TenantFollowupSettings.tenant_id == tenant.id))).scalar_one()
    eligible = await find_eligible_conversations(db_session, tenant.id, settings)
    assert eligible == []


@pytest.mark.asyncio
async def test_conversation_eligible_after_first_threshold(db_session, unique_email):
    tenant, customer = await _setup(db_session, unique_email)
    conv = Conversation(
        tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
        last_message_at=_now() - timedelta(hours=25),  # au-delà de 24h
    )
    db_session.add(conv)
    await db_session.commit()

    from sqlalchemy import select

    settings = (await db_session.execute(select(TenantFollowupSettings).where(TenantFollowupSettings.tenant_id == tenant.id))).scalar_one()
    eligible = await find_eligible_conversations(db_session, tenant.id, settings)
    assert len(eligible) == 1


@pytest.mark.asyncio
async def test_waiting_human_conversation_never_eligible(db_session, unique_email):
    tenant, customer = await _setup(db_session, unique_email)
    conv = Conversation(
        tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.WAITING_HUMAN,
        last_message_at=_now() - timedelta(hours=100),
    )
    db_session.add(conv)
    await db_session.commit()

    from sqlalchemy import select

    settings = (await db_session.execute(select(TenantFollowupSettings).where(TenantFollowupSettings.tenant_id == tenant.id))).scalar_one()
    eligible = await find_eligible_conversations(db_session, tenant.id, settings)
    assert eligible == []  # jamais de relance sur une conversation prise en main par un humain


@pytest.mark.asyncio
async def test_stage_two_never_eligible_again_stop(db_session, unique_email):
    """Section 23 — STOP après la deuxième relance, jamais de troisième."""
    tenant, customer = await _setup(db_session, unique_email)
    conv = Conversation(
        tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
        last_message_at=_now() - timedelta(hours=1000),
        followup_stage=2,
        last_followup_at=_now() - timedelta(hours=1000),
    )
    db_session.add(conv)
    await db_session.commit()

    from sqlalchemy import select

    settings = (await db_session.execute(select(TenantFollowupSettings).where(TenantFollowupSettings.tenant_id == tenant.id))).scalar_one()
    eligible = await find_eligible_conversations(db_session, tenant.id, settings)
    assert eligible == []


@pytest.mark.asyncio
async def test_run_followups_sends_and_advances_stage(db_session, unique_email):
    tenant, customer = await _setup(db_session, unique_email)
    conv = Conversation(
        tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
        last_message_at=_now() - timedelta(hours=25),
    )
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)

    sent = await run_followups_for_tenant(db_session, tenant.id)
    assert sent == 1

    await db_session.refresh(conv)
    assert conv.followup_stage == 1
    assert conv.last_followup_at is not None

    from sqlalchemy import select

    from app.models.conversation import Message

    messages = (await db_session.execute(select(Message).where(Message.conversation_id == conv.id))).scalars().all()
    assert any(m.message_type == "followup" for m in messages)


@pytest.mark.asyncio
async def test_run_followups_skipped_when_disabled(db_session, unique_email):
    tenant, customer = await _setup(db_session, unique_email, followup_enabled=False)
    conv = Conversation(
        tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
        last_message_at=_now() - timedelta(hours=100),
    )
    db_session.add(conv)
    await db_session.commit()

    sent = await run_followups_for_tenant(db_session, tenant.id)
    assert sent == 0


@pytest.mark.asyncio
async def test_run_followups_skipped_when_outbound_not_commercial(db_session, unique_email):
    """Respecte le garde-fou section 56 : sans mode commercial activé, pas d'envoi proactif."""
    tenant, customer = await _setup(db_session, unique_email, outbound_mode=OutboundMode.AI_PLUS_HUMAN)
    conv = Conversation(
        tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
        last_message_at=_now() - timedelta(hours=100),
    )
    db_session.add(conv)
    await db_session.commit()

    sent = await run_followups_for_tenant(db_session, tenant.id)
    assert sent == 0


@pytest.mark.asyncio
async def test_second_followup_after_first(db_session, unique_email):
    tenant, customer = await _setup(db_session, unique_email)
    conv = Conversation(
        tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
        last_message_at=_now() - timedelta(hours=200),
        followup_stage=1,
        last_followup_at=_now() - timedelta(hours=73),  # au-delà des 72h configurées
    )
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)

    sent = await run_followups_for_tenant(db_session, tenant.id)
    assert sent == 1
    await db_session.refresh(conv)
    assert conv.followup_stage == 2
