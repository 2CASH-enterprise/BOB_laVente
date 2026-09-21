"""
Relance automatique des conversations abandonnées (section 23).

Séquence : conversation sans réponse -> attente N1 heures -> première relance ->
attente N2 heures -> deuxième relance -> STOP (jamais de troisième relance).

Chaque envoi passe par le même garde-fou que tout envoi proactif (section 56) :
permission CAN_SEND_PROACTIVE_MESSAGE, qui exige outbound_mode == COMMERCIAL_ENABLED.
Une relance n'est donc jamais envoyée si le tenant n'a pas explicitement activé les
envois commerciaux — cohérent avec la maîtrise des coûts WhatsApp déjà en place.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.whatsapp.client import WhatsAppClient
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.followup_settings import TenantFollowupSettings
from app.models.whatsapp_account import WhatsAppAccount
from app.services.messaging_guard import OutboundDenied, Permission, check_and_log_outbound

logger = logging.getLogger(__name__)


def _ensure_aware(dt: datetime) -> datetime:
    """SQLite (tests) renvoie parfois des datetime naïfs même sur une colonne timezone=True ;
    PostgreSQL (production) les renvoie toujours conscients du fuseau. On uniformise en UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def find_eligible_conversations(
    db: AsyncSession, tenant_id, settings: TenantFollowupSettings, now: datetime | None = None
) -> list[Conversation]:
    """
    Ne renvoie que les conversations réellement éligibles à CE moment précis : jamais
    WAITING_HUMAN (un humain est déjà dessus, section 28) ni CLOSED, jamais avant le
    délai configuré, jamais au-delà de la deuxième relance.
    """
    now = _ensure_aware(now or datetime.now(timezone.utc))

    stmt = select(Conversation).where(
        Conversation.tenant_id == tenant_id,
        Conversation.status == ConversationStatus.ACTIVE,
        Conversation.last_message_at.is_not(None),
    )
    candidates = list((await db.execute(stmt)).scalars().all())

    eligible = []
    for conv in candidates:
        if conv.followup_stage == 0:
            threshold = _ensure_aware(conv.last_message_at) + timedelta(hours=settings.first_followup_hours)
            if now >= threshold:
                eligible.append(conv)
        elif conv.followup_stage == 1:
            reference = conv.last_followup_at or conv.last_message_at
            threshold = _ensure_aware(reference) + timedelta(hours=settings.second_followup_hours)
            if now >= threshold:
                eligible.append(conv)
        # followup_stage >= 2 : STOP, jamais de relance supplémentaire (section 23)

    return eligible


async def send_followup(
    db: AsyncSession,
    tenant_id,
    conversation: Conversation,
    settings: TenantFollowupSettings,
    now: datetime | None = None,
) -> bool:
    """Retourne True si la relance a bien été envoyée, False si refusée par les garde-fous."""
    now = now or datetime.now(timezone.utc)
    message_text = settings.first_message if conversation.followup_stage == 0 else settings.second_message

    try:
        await check_and_log_outbound(
            db, tenant_id=tenant_id, requested_by="SYSTEM", permission=Permission.CAN_SEND_PROACTIVE_MESSAGE
        )
    except OutboundDenied:
        await db.commit()
        return False

    account_stmt = select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id)
    account = (await db.execute(account_stmt)).scalar_one_or_none()
    customer = await db.get(Customer, conversation.customer_id)

    if account is not None and customer is not None:
        try:
            wa_client = WhatsAppClient(phone_number_id=account.phone_number_id, system_user_token=account.system_user_token)
            await wa_client.send_text_message(to=customer.whatsapp_number, body=message_text)
        except Exception:  # noqa: BLE001 — jamais interrompre le traitement des autres conversations
            logger.exception("Échec d'envoi de relance pour la conversation %s", conversation.id)

    db.add(
        Message(
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            sender=MessageSender.SYSTEM,
            message_type="followup",
            content=message_text,
        )
    )
    conversation.followup_stage += 1
    conversation.last_followup_at = now
    await db.commit()
    return True


async def run_followups_for_tenant(db: AsyncSession, tenant_id, now: datetime | None = None) -> int:
    """Retourne le nombre de relances effectivement envoyées pour ce tenant."""
    settings_stmt = select(TenantFollowupSettings).where(TenantFollowupSettings.tenant_id == tenant_id)
    settings = (await db.execute(settings_stmt)).scalar_one_or_none()

    if settings is None or not settings.enabled:
        return 0

    eligible = await find_eligible_conversations(db, tenant_id, settings, now=now)
    sent_count = 0
    for conversation in eligible:
        if await send_followup(db, tenant_id, conversation, settings, now=now):
            sent_count += 1
    return sent_count
