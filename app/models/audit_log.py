import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AuditLog(Base):
    """
    Section 32/38 — trace des actions sensibles. tenant_id est nullable pour couvrir
    les événements pré-authentification (ex. tentative de connexion échouée) qui ne
    peuvent pas toujours être rattachés à un tenant identifié.
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)  # user_id, "IA", ou "ANONYMOUS"
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    details: Mapped[dict | None] = mapped_column(JSON)
    ip_address: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
