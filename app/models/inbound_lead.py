import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InboundLead(Base):
    """
    Lot 83 — demande reçue par email pour une agence immobilière : contact d'un portail (SeLoger, Leboncoin, Bien'ici,
    Logic-Immo, PAP) ou du formulaire de son site, que l'agence fait suivre à son adresse Bob.

    status : DRAFT (réponse de Bob prête, à valider), SENT (réponse envoyée), TO_HANDLE (à traiter par l'agent : email
    mal reconnu, plafond, verrou), IGNORED (écartée par l'agent), FAILED (envoi impossible).
    """

    __tablename__ = "inbound_leads"
    __table_args__ = (UniqueConstraint("tenant_id", "message_id", name="uq_inbound_lead_message"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
                                                 nullable=False, index=True)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="SITE")
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="TO_HANDLE", index=True)
    subject: Mapped[str | None] = mapped_column(String(255))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(String(64))
    product_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"))
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"))
    reply_subject: Mapped[str | None] = mapped_column(String(255))
    reply_text: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(String(300))
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LeadInboxState(Base):
    """Lot 83 — où en est la lecture de la boîte des demandes (une seule ligne, id = 1)."""

    __tablename__ = "lead_inbox_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_uid: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(300))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
