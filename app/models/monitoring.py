"""
Lot 67 — monitoring technique : les erreurs de Bob regroupées par « signature » (même source, même endroit du code,
même message une fois les nombres et identifiants retirés), et leurs dernières occurrences. Tout ce qui est
enregistré ici est déjà masqué (jetons, mots de passe, numéros) : voir app/services/monitoring.mask.
Conservation : 30 jours (tâche de purge quotidienne).
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MonitorIssue(Base):
    __tablename__ = "monitor_issues"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signature: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    # API, TASK, SEND_WHATSAPP, SEND_EMAIL, AI, WEBHOOK, BROWSER, APP
    source: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    exc_type: Mapped[str | None] = mapped_column(String(120))
    location: Mapped[str | None] = mapped_column(String(300))   # fichier:ligne (fonction) — dernière occurrence
    context: Mapped[str | None] = mapped_column(String(300))    # route, tâche ou page
    meta_code: Mapped[str | None] = mapped_column(String(16))   # code d'erreur Meta (envois WhatsApp)
    # NEW (Nouvelle), IN_PROGRESS (En cours), RESOLVED (Corrigée), REOPENED (Revenue après correction)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="NEW", server_default="NEW", index=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_message: Mapped[str | None] = mapped_column(String(1000))
    last_trace: Mapped[str | None] = mapped_column(Text)
    tenant_ids: Mapped[list | None] = mapped_column(JSON)       # boutiques touchées (20 au plus)
    browsers: Mapped[list | None] = mapped_column(JSON)         # navigateurs (erreurs du navigateur)
    assignee: Mapped[str | None] = mapped_column(String(255))
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(255))
    reopened_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))        # email « nouvelle erreur »
    spike_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # email « pic »
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MonitorOccurrence(Base):
    __tablename__ = "monitor_occurrences"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("monitor_issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    message: Mapped[str | None] = mapped_column(String(500))
    context: Mapped[str | None] = mapped_column(String(300))
