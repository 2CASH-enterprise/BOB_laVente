import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InsuranceComplaint(Base):
    """
    Lot 57 — registre des réclamations et des sinistres du courtier (règlement CIMA 01-24, art. 11 : dispositif
    accessible par plusieurs canaux, délai de traitement, traçabilité des réclamations reçues et traitées).

    Créée par le CODE quand Bob reconnaît une réclamation ou un sinistre sur WhatsApp (jamais par l'IA), ou à la
    main par le cabinet (téléphone, email, visite). Une seule fiche ouverte par client : les messages suivants
    s'y rattachent. Statuts : RECEIVED (reçue) → IN_PROGRESS (en cours) → RESOLVED (traitée, avec la réponse
    apportée). Chaque étape est horodatée. due_on : date limite de réponse (réclamations seulement).
    """

    __tablename__ = "insurance_complaints"
    __table_args__ = (UniqueConstraint("tenant_id", "reference", name="uq_insurance_complaints_reference"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    reference: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="RECLAMATION")  # RECLAMATION, SINISTRE
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="WHATSAPP")  # WHATSAPP, PHONE, EMAIL, OFFICE, OTHER
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RECEIVED", server_default="RECEIVED", index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_on: Mapped[date | None] = mapped_column(Date)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    follow_ups: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolution: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="BOB")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
