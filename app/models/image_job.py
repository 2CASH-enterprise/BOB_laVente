import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ImageJob(Base):
    """Lot 87 — chaque retouche de photo (fond blanc ou photo portée) : quotas du mois et coût réel."""

    __tablename__ = "image_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
                                                 nullable=False, index=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"),
                                                         index=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)      # WHITE, WORN
    engine: Mapped[str] = mapped_column(String(16), nullable=False)   # PHOTOROOM, SERVER, FASHN
    status: Mapped[str] = mapped_column(String(16), nullable=False)   # PENDING, DONE, FAILED
    external_id: Mapped[str | None] = mapped_column(String(128))      # identifiant FASHN
    cost_usd: Mapped[float] = mapped_column(Numeric(8, 4), default=0, server_default="0", nullable=False)
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
