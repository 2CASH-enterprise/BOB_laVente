"""
Lot 43 — fiche prospect d'une concession : ce que Bob a appris d'un prospect, avec ou sans
rendez-vous. Une fiche par client et par concession.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ProspectProfile(Base):
    __tablename__ = "prospect_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", "customer_id", name="uq_prospect_profile_customer"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    need: Mapped[str | None] = mapped_column(String(300))          # type de véhicule, usage (« SUV familial »)
    condition: Mapped[str | None] = mapped_column(String(16))      # NEUF, OCCASION, INDIFFERENT
    budget: Mapped[str | None] = mapped_column(String(100))        # tel que donné par le client
    payment: Mapped[str | None] = mapped_column(String(16))        # COMPTANT, FINANCEMENT, A_DEFINIR
    timeline: Mapped[str | None] = mapped_column(String(16))       # MOINS_1_MOIS, UN_A_3_MOIS, PLUS_3_MOIS, NE_SAIT_PAS
    trade_in: Mapped[str | None] = mapped_column(String(300))      # véhicule à reprendre
    hot_alert_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # « prospect chaud », une fois
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
