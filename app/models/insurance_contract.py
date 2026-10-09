import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InsuranceContract(Base):
    """
    Lot 55 — registre des contrats du cabinet (courtier / agent d'assurance), pour la rétention.

    Saisi par le cabinet (tableau de bord ou import CSV), jamais par Bob. Statuts : ACTIVE (en cours),
    RENEWED (renouvelé : une nouvelle ligne prend la suite, renewed_from_id), CANCELLED (résilié),
    LOST (parti chez un concurrent), EXPIRED (échu sans suite).

    Règlement CIMA : la prime et le numéro de contrat restent dans le tableau de bord. Ils ne sont jamais
    envoyés à l'IA ni écrits dans un message au client ; la prime n'est visible que des administrateurs.

    Échéance : alerte au cabinet (broker_alerted_at, une fois) puis rappel au client (client_reminded_at,
    une fois, par WhatsApp si la fenêtre de 20 h est ouverte, sinon par email, sinon « à appeler »).
    « Prise en charge » (renewal_handled_at) : le cabinet s'en occupe, plus de rappel automatique au client.
    """

    __tablename__ = "insurance_contracts"
    __table_args__ = (Index("ix_insurance_contracts_tenant_expiry", "tenant_id", "expires_on"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quote_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("quote_requests.id", ondelete="SET NULL")
    )
    renewed_from_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("insurance_contracts.id", ondelete="SET NULL")
    )
    branch: Mapped[str] = mapped_column(String(32), nullable=False)
    insurer: Mapped[str | None] = mapped_column(String(150))
    policy_number: Mapped[str | None] = mapped_column(String(64))
    premium: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    effective_on: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date] = mapped_column(Date, nullable=False)
    term: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE", server_default="ACTIVE", index=True)
    note: Mapped[str | None] = mapped_column(String(500))

    broker_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_reminder_channel: Mapped[str | None] = mapped_column(String(16))  # WHATSAPP, EMAIL, CALL
    renewal_handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    renewal_handled_by: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
