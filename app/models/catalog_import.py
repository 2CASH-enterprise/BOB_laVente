import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CatalogImport(Base):
    """Lot 88 — import d'un catalogue PDF : quotas du mois (lecture par l'IA), coût réel, aperçu à vérifier."""

    __tablename__ = "catalog_imports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
                                                 nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(12), nullable=False)   # PDF_TABLE (lu sans IA), PDF_AI (Mistral)
    status: Mapped[str] = mapped_column(String(12), nullable=False)   # PROCESSING, READY, FAILED, IMPORTED
    filename: Mapped[str | None] = mapped_column(String(200))
    pages: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(8, 4), default=0, server_default="0", nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON)                 # {"header": [...], "rows": [[...]]}
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
