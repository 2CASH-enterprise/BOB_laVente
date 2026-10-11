"""
Lot 68 — modèles de message WhatsApp (approuvés par Meta) d'une boutique : un par modèle et par forme
(vous / tu). Le statut suit celui de Meta (webhook « message_template_status_update » et vérification
toutes les heures). Bob n'envoie un modèle que s'il est APPROVED et de catégorie UTILITY.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WhatsAppTemplate(Base):
    __tablename__ = "whatsapp_templates"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_whatsapp_templates_tenant_name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(40), nullable=False)       # rdv_rappel, commande_paiement…
    form: Mapped[str] = mapped_column(String(8), nullable=False)       # VOUS ou TU
    name: Mapped[str] = mapped_column(String(80), nullable=False)      # bob_rdv_rappel_vous (nom chez Meta)
    meta_id: Mapped[str | None] = mapped_column(String(64))
    # NOT_CREATED, PENDING, APPROVED, REJECTED, PAUSED, DISABLED, ERROR
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="NOT_CREATED", server_default="NOT_CREATED")
    category: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
