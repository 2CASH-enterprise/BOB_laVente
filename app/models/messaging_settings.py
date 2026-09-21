import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OutboundMode(StrEnum):
    """Section 56.3"""

    AI_ONLY = "AI_ONLY"
    AI_PLUS_HUMAN = "AI_PLUS_HUMAN"
    COMMERCIAL_ENABLED = "COMMERCIAL_ENABLED"


class KillSwitch(StrEnum):
    """Section 56.6 — prioritaire sur outbound_mode."""

    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"


class TenantMessagingSettings(Base):
    __tablename__ = "tenant_messaging_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )

    outbound_mode: Mapped[OutboundMode] = mapped_column(
        Enum(OutboundMode, name="outbound_mode"), default=OutboundMode.AI_ONLY, nullable=False
    )
    kill_switch: Mapped[KillSwitch] = mapped_column(
        Enum(KillSwitch, name="kill_switch"), default=KillSwitch.ALLOWED, nullable=False
    )
    daily_outbound_limit: Mapped[int] = mapped_column(Integer, default=100)
    templates_only: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SendResult(StrEnum):
    SENT = "SENT"
    DENIED_403 = "DENIED_403"
    DENIED_QUOTA = "DENIED_QUOTA"


class MessageSendAudit(Base):
    """Section 56.7 — journalisation de CHAQUE tentative d'envoi, acceptée ou refusée."""

    __tablename__ = "message_send_audit"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)  # "IA", user_id, ou "SYSTEM"
    permission_checked: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[SendResult] = mapped_column(Enum(SendResult, name="send_result"), nullable=False)
    send_date: Mapped[date] = mapped_column(Date, default=date.today, index=True)  # pour le calcul du quota journalier

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
