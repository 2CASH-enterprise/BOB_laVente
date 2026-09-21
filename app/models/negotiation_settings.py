import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TenantNegotiationSettings(Base):
    """
    Section 52.2 — règles de négociation B2C, configurables par entreprise. Une seule règle
    globale par tenant pour ce MVP (les règles par catégorie, prévues section 52.2,
    pourront s'ajouter plus tard sans changer cette table).
    """

    __tablename__ = "tenant_negotiation_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_negotiation_settings_tenant"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_discount_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=10.0, nullable=False)
    max_rounds: Mapped[int] = mapped_column(default=3, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
