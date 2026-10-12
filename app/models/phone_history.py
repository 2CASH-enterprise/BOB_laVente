import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PhoneContact(Base):
    """
    Lot 52 — contact du téléphone du commerçant (coexistence, webhook `smb_app_state_sync`). Sert UNIQUEMENT
    à nommer un client quand il écrit : jamais de fiche client créée pour un contact, jamais d'offre envoyée.
    """

    __tablename__ = "phone_contacts"
    __table_args__ = (UniqueConstraint("tenant_id", "wa_number", name="uq_phone_contacts_tenant_number"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    wa_number: Mapped[str] = mapped_column(String(32), nullable=False)  # chiffres seuls, comme Customer.whatsapp_number
    full_name: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PhoneHistoryMessage(Base):
    """
    Lot 52 — message des 6 derniers mois repris du téléphone (coexistence, webhook `history`). Rangé par numéro
    et jamais dans une conversation : aucune réponse, analyse, relance, statistique ni quota. Montré au
    commerçant dans la conversation du client, et donné à Bob comme mémoire.
    """

    __tablename__ = "phone_history_messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "wa_message_id", name="uq_phone_history_tenant_wamid"),
        Index("ix_phone_history_tenant_number_sent", "tenant_id", "wa_number", "sent_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    wa_number: Mapped[str] = mapped_column(String(32), nullable=False)  # le client (chiffres seuls)
    from_business: Mapped[bool] = mapped_column(Boolean, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    wa_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
