"""
Demande de rendez-vous (lot 24, mode concession) : enregistrée par Bob, TOUJOURS confirmée par
un humain. Bob ne confirme jamais un rendez-vous lui-même : il transmet la demande.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

APPOINTMENT_KINDS = {
    "ESSAI": "Essai",
    "VISITE": "Visite",
    "ESTIMATION_REPRISE": "Estimation de reprise",
    "CABINET": "Rendez-vous au cabinet",  # lot 53 : courtier / agent d'assurance
    "APPEL": "Appel d'un conseiller",
}
STATUS_REQUESTED = "REQUESTED"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_CANCELLED = "CANCELLED"

# Lot 36 — issue d'un rendez-vous passé, saisie par le conseiller.
OUTCOME_SOLD = "SOLD"
OUTCOME_FOLLOW_UP = "FOLLOW_UP"
OUTCOME_NOT_INTERESTED = "NOT_INTERESTED"
OUTCOME_NO_SHOW = "NO_SHOW"
OUTCOMES = {
    OUTCOME_SOLD: "Venu, vendu",
    OUTCOME_FOLLOW_UP: "Venu, à relancer",
    OUTCOME_NOT_INTERESTED: "Venu, pas intéressé",
    OUTCOME_NO_SHOW: "Absent",
}
VISITED_OUTCOMES = frozenset({OUTCOME_SOLD, OUTCOME_FOLLOW_UP, OUTCOME_NOT_INTERESTED})


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

    # Lot 25 — fiche de qualification remplie par Bob (plus de notes en vrac).
    need: Mapped[str | None] = mapped_column(String(300))  # usage, besoin (« voiture familiale »)
    budget: Mapped[str | None] = mapped_column(String(100))  # tel que donné par le client
    trade_in: Mapped[str | None] = mapped_column(String(300))  # véhicule à reprendre : marque, modèle, année, km
    financing_interest: Mapped[bool | None] = mapped_column(Boolean)  # None = non abordé

    # Lot 25 — confirmation par un humain, rappels.
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)  # UTC
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(64))  # user_id
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # rappel au conseiller
    customer_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Lot 35 — rappel WhatsApp au prospect (seulement si la conversation est ouverte), et
    # changements faits par le prospect lui-même en écrivant à Bob.
    customer_whatsapp_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[str | None] = mapped_column(String(64))  # "CLIENT" ou user_id
    rescheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rescheduled_by: Mapped[str | None] = mapped_column(String(64))  # "CLIENT" ou user_id

    # Lot 36 — issue du rendez-vous et relance (une seule, jamais plus).
    outcome: Mapped[str | None] = mapped_column(String(16))
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome_by: Mapped[str | None] = mapped_column(String(64))
    followup_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followup_channel: Mapped[str | None] = mapped_column(String(16))  # "WHATSAPP", "EMAIL", "TASK"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
