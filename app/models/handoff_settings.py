import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

COMPLAINT_TRY_FIRST = "TRY_FIRST"   # Bob tente de résoudre, peut transférer s'il n'y arrive pas
COMPLAINT_TRANSFER = "TRANSFER"     # transfert immédiat

DISCOUNT_FIXED_PRICES = "FIXED_PRICES"  # négociation désactivée : Bob indique poliment que les prix sont fixes
DISCOUNT_TRANSFER = "TRANSFER"          # négociation désactivée : transfert immédiat


class TenantHandoffSettings(Base):
    """
    Réglages des règles de transmission à un humain (lot 13). Absence de ligne = valeurs par
    défaut : aucune donnée existante n'a besoin d'être modifiée. « Demande d'un humain » n'est
    volontairement PAS réglable : un client qui demande une personne doit l'obtenir.
    """

    __tablename__ = "tenant_handoff_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    refund_transfer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    complaint_policy: Mapped[str] = mapped_column(String(16), nullable=False, default=COMPLAINT_TRY_FIRST)
    discount_policy: Mapped[str] = mapped_column(String(16), nullable=False, default=DISCOUNT_FIXED_PRICES)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
