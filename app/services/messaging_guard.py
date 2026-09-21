"""
Vérification des envois sortants (section 56.5).

Principe : ce contrôle est appelé UNE SEULE FOIS, ici, quel que soit l'appelant
(dashboard, API publique, ou l'IA elle-même). Aucune route de la plateforme
n'est autorisée à appeler l'API WhatsApp directement sans passer par cette
fonction — c'est elle qui journalise systématiquement le résultat dans
message_send_audit (section 56.7), y compris les refus.
"""
from datetime import date
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.messaging_settings import (
    KillSwitch,
    MessageSendAudit,
    OutboundMode,
    SendResult,
    TenantMessagingSettings,
)


class Permission(StrEnum):
    """Section 56.4"""

    CAN_REPLY_TO_CUSTOMER = "CAN_REPLY_TO_CUSTOMER"
    CAN_SEND_PROACTIVE_MESSAGE = "CAN_SEND_PROACTIVE_MESSAGE"


class OutboundDenied(Exception):
    def __init__(self, reason: str, http_status: int = 403):
        self.reason = reason
        self.http_status = http_status
        super().__init__(reason)


async def check_and_log_outbound(
    db: AsyncSession,
    tenant_id,
    requested_by: str,
    permission: Permission,
) -> None:
    """
    Lève OutboundDenied si l'envoi n'est pas autorisé. Ne renvoie rien en cas de succès
    (l'appelant peut alors procéder à l'appel WhatsApp réel).
    """
    stmt = select(TenantMessagingSettings).where(TenantMessagingSettings.tenant_id == tenant_id)
    result = await db.execute(stmt)
    settings = result.scalar_one_or_none()

    # Pas de configuration explicite = comportement par défaut du produit : IA uniquement,
    # kill switch désactivé (section 56.2). Un humain reste bloqué tant que rien n'est configuré ;
    # seule l'IA peut répondre — jamais un refus total qui empêcherait le vendeur IA de fonctionner.
    if settings is None:
        settings = TenantMessagingSettings(
            tenant_id=tenant_id, outbound_mode=OutboundMode.AI_ONLY, kill_switch=KillSwitch.ALLOWED
        )
        db.add(settings)
        await db.flush()

    if settings.kill_switch == KillSwitch.BLOCKED:
        await _log(db, tenant_id, requested_by, permission, SendResult.DENIED_403)
        raise OutboundDenied("Kill switch plateforme actif pour ce tenant")

    if permission == Permission.CAN_SEND_PROACTIVE_MESSAGE and settings.outbound_mode != OutboundMode.COMMERCIAL_ENABLED:
        await _log(db, tenant_id, requested_by, permission, SendResult.DENIED_403)
        raise OutboundDenied("Envoi proactif désactivé pour ce tenant (mode outbound insuffisant)")

    if permission == Permission.CAN_REPLY_TO_CUSTOMER and settings.outbound_mode == OutboundMode.AI_ONLY and requested_by != "IA":
        await _log(db, tenant_id, requested_by, permission, SendResult.DENIED_403)
        raise OutboundDenied("Seule l'IA peut répondre pour ce tenant (mode IA uniquement)")

    # Quota journalier (uniquement pertinent pour les envois proactifs / commerciaux)
    if permission == Permission.CAN_SEND_PROACTIVE_MESSAGE:
        count_stmt = select(func.count()).where(
            MessageSendAudit.tenant_id == tenant_id,
            MessageSendAudit.send_date == date.today(),
            MessageSendAudit.result == SendResult.SENT,
        )
        sent_today = (await db.execute(count_stmt)).scalar_one()
        if sent_today >= settings.daily_outbound_limit:
            await _log(db, tenant_id, requested_by, permission, SendResult.DENIED_QUOTA)
            raise OutboundDenied("Quota journalier d'envois atteint", http_status=429)

    await _log(db, tenant_id, requested_by, permission, SendResult.SENT)


async def _log(db: AsyncSession, tenant_id, requested_by: str, permission: Permission, result: SendResult) -> None:
    db.add(
        MessageSendAudit(
            tenant_id=tenant_id,
            requested_by=requested_by,
            permission_checked=permission.value,
            result=result,
        )
    )
    await db.flush()
