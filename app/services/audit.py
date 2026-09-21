import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog


async def log_audit_event(
    db: AsyncSession,
    *,
    actor: str,
    action: str,
    tenant_id: uuid.UUID | None = None,
    details: dict | None = None,
    ip_address: str | None = None,
) -> None:
    """
    N'échoue jamais bruyamment : l'audit ne doit pas faire tomber une requête métier.
    En cas de problème (ex. session déjà en erreur), on avale l'exception après flush.
    """
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            details=details,
            ip_address=ip_address,
        )
    )
    await db.flush()
