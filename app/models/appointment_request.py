"""
Demande de rendez-vous (lot 24, mode concession) : enregistrée par Bob, TOUJOURS confirmée par
un humain. Bob ne confirme jamais un rendez-vous lui-même : il transmet la demande.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

APPOINTMENT_KINDS = {
    "ESSAI": "Essai",
    "VISITE": "Visite",
    "ESTIMATION_REPRISE": "Estimation de reprise",
}
STATUS_REQUESTED = "REQUESTED"


class AppointmentRequest(Base):
    __tablename__ = "appointment_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    vehicle_label: Mapped[str | None] = mapped_column(String(255))  # véhicule tel que le client l'a désigné
    availability: Mapped[str] = mapped_column(String(300), nullable=False)  # texte libre (« samedi matin »)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_REQUESTED)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
