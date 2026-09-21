import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NegotiationStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    ACCEPTED = "ACCEPTED"
    ESCALATED = "ESCALATED"


class Negotiation(Base):
    """Section 52.3 — trace chaque round de négociation, jamais recalculée depuis zéro."""

    __tablename__ = "negotiations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("products.id"), nullable=False)

    initial_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    last_proposed_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    final_price: Mapped[float | None] = mapped_column(Numeric(14, 2))
    rounds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[NegotiationStatus] = mapped_column(
        Enum(NegotiationStatus, name="negotiation_status"), default=NegotiationStatus.IN_PROGRESS
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
