import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Numeric, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MessageSignal(Base):
    """
    Étiquettes déduites d'un message client (phase 1) : intentions, objections, prix proposé.
    Toujours une DÉDUCTION du classificateur, jamais un fait vérifié. Seules les étiquettes
    sont stockées, jamais une copie du texte du message.
    """

    __tablename__ = "message_signals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    intents: Mapped[list] = mapped_column(JSON, nullable=False)
    objections: Mapped[list] = mapped_column(JSON, nullable=False)
    offered_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(8), nullable=False)
    message_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
