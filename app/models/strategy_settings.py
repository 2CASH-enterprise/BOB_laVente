import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TenantStrategySettings(Base):
    """
    Stratégies de réponse aux objections DÉSACTIVÉES par le commerce (lot 15). On stocke les
    désactivations plutôt que les activations : une stratégie ajoutée plus tard à la bibliothèque
    est active par défaut, sans migration de données. Absence de ligne = tout est activé.
    """

    __tablename__ = "tenant_strategy_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    disabled_strategies: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
