import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

DEFAULT_FIRST_MESSAGE = "Bonjour, êtes-vous toujours intéressé(e) ? Je reste disponible pour répondre à vos questions."
DEFAULT_SECOND_MESSAGE = "Petit rappel : je suis toujours là si vous souhaitez reprendre notre échange."


class TenantFollowupSettings(Base):
    """
    Section 23 — règles de relance configurables par entreprise. Désactivées par défaut
    (enabled=False) : cohérent avec le principe de la section 56 — aucun envoi proactif
    sans activation explicite du tenant.
    """

    __tablename__ = "tenant_followup_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_followup_settings_tenant"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_followup_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    first_message: Mapped[str] = mapped_column(Text, default=DEFAULT_FIRST_MESSAGE, nullable=False)
    second_followup_hours: Mapped[int] = mapped_column(Integer, default=72, nullable=False)
    second_message: Mapped[str] = mapped_column(Text, default=DEFAULT_SECOND_MESSAGE, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
