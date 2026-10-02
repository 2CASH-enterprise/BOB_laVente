import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LlmUsage(Base):
    """
    Lot 50 — un appel au modèle d'IA : boutique, usage, modèle et tokens. Jamais le texte échangé.
    Sert à connaître le coût réel par boutique (Super Admin) et à vérifier que le cache fonctionne.
    """

    __tablename__ = "llm_usage"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # REPLY (réponse de Bob) | CLASSIFIER
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class LlmBudgetAlert(Base):
    """Lot 50 — alerte « budget IA dépassé » : une seule par boutique et par mois."""

    __tablename__ = "llm_budget_alerts"
    __table_args__ = (UniqueConstraint("tenant_id", "month", name="uq_llm_budget_alert_month"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    month: Mapped[str] = mapped_column(String(7), nullable=False)  # « 2026-10 »
    cost_usd: Mapped[str] = mapped_column(String(16), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
