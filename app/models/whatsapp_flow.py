"""
Lot 114 — formulaires WhatsApp (Flows) d'un restaurant : un par usage (« booking » = réserver une table), créé et
publié chez Meta sur le compte WhatsApp du restaurant au premier besoin. Statut : NOT_CREATED, PUBLISHED, ERROR.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WhatsAppFlow(Base):
    __tablename__ = "whatsapp_flows"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_whatsapp_flows_tenant_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str | None] = mapped_column(String(80))
    meta_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="NOT_CREATED", server_default="NOT_CREATED")
    reason: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
