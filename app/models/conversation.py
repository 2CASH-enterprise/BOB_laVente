import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, JSON, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ConversationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    WAITING_CUSTOMER = "WAITING_CUSTOMER"
    WAITING_HUMAN = "WAITING_HUMAN"
    CLOSED = "CLOSED"


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[ConversationStatus] = mapped_column(
        Enum(ConversationStatus, name="conversation_status"), default=ConversationStatus.ACTIVE
    )
    assigned_agent: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Section 23 — relance automatique. 0 = aucune relance envoyée, 1 = première envoyée,
    # 2 = deuxième envoyée (STOP, plus aucune relance après). Remis à 0 dès que le client
    # répond (la conversation n'est alors plus « abandonnée »).
    followup_stage: Mapped[int] = mapped_column(default=0, nullable=False)
    last_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Dernière alerte email envoyée au commerçant pour cette attente (transfert ou rappel) :
    # permet de limiter les rappels à un par heure.
    human_alert_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MessageSender(StrEnum):
    CUSTOMER = "CUSTOMER"
    AI = "AI"
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    sender: Mapped[MessageSender] = mapped_column(Enum(MessageSender, name="message_sender"), nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), default="text")  # text, image, button, list, location
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_metadata: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
