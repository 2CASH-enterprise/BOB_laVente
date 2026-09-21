import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class KnowledgeCategory(StrEnum):
    """Section 29 — catégories prévues, plus OBJECTION pour le traitement des objections commerciales."""

    HORAIRES = "HORAIRES"
    ADRESSE = "ADRESSE"
    LIVRAISON = "LIVRAISON"
    RETOUR = "RETOUR"
    GARANTIE = "GARANTIE"
    PAIEMENT = "PAIEMENT"
    FAQ = "FAQ"
    CONDITIONS = "CONDITIONS"
    OBJECTION = "OBJECTION"
    AUTRE = "AUTRE"


class KnowledgeEntry(Base):
    """
    Section 29 — base de connaissances consultée par l'agent IA à chaque conversation
    (injectée dans le prompt système, section 20). Mise à jour à tout moment par
    l'entreprise depuis le dashboard, sans redéploiement.
    """

    __tablename__ = "knowledge_entries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    category: Mapped[KnowledgeCategory] = mapped_column(
        Enum(KnowledgeCategory, name="knowledge_category"), nullable=False, default=KnowledgeCategory.AUTRE
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
