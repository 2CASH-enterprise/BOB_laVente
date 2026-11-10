"""Lot 120 — interrupteur entre les Super Admin (contrat 1.5, section « console »)."""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ConsoleJump(Base):
    """Jeton de passage déjà servi (usage unique) ; effacé après expiration."""

    __tablename__ = "console_jumps"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class ConsoleEvent(Base):
    """Changement d'un membre de l'équipe AgenC'AI à transmettre aux autres consoles (Naomy), avec renvois."""

    __tablename__ = "console_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent: Mapped[str] = mapped_column(String(16), nullable=False, default="naomy")
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
