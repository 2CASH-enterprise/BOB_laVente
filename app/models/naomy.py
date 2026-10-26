"""
Lot 89 — connecteur Naomy (contrat « Passerelle Bob ↔ Naomy » 1.2, fichier docs/naomy/bob-naomy-openapi-v1.2.yaml).

- NaomyConnection : une liaison d'une boutique avec un compte Naomy. Une seule ACTIVE par boutique ; les anciennes
  restent (CLOSED) pour envoyer `connexion.fermee` avec leur propre adresse et leur propre secret. La clé de l'API
  n'est gardée qu'en empreinte (SHA-256) ; le secret des événements est chiffré (il sert à signer).
- NaomyPairingCode : code d'appairage à usage unique (15 minutes), gardé en empreinte.
- NaomyEvent : événements à envoyer à Naomy (file d'attente en base, envoyée chaque minute, avec renvois).
- NaomyReminder : rappels par e-mail demandés par Naomy (idempotence et « 1 par boutique et par jour »).
"""
import uuid
from datetime import date, datetime

from sqlalchemy import (JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, func,
                        true as sa_true)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedString
from app.core.database import Base


class NaomyConnection(Base):
    __tablename__ = "naomy_connections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
                                                 nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="ACTIVE")  # ACTIVE, CLOSED
    naomy_account: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_number: Mapped[str] = mapped_column(String(20), nullable=False)  # numéro qui a confirmé (E.164)
    events_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    events_secret: Mapped[str] = mapped_column(EncryptedString(512), nullable=False)
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    reminders_unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[str | None] = mapped_column(String(8))  # BOB, NAOMY


class NaomyPairingCode(Base):
    __tablename__ = "naomy_pairing_codes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
                                                 nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class NaomyEvent(Base):
    __tablename__ = "naomy_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
                                                 nullable=False, index=True)
    # Événements de connexion : la liaison précise (adresse et secret) ; les autres partent vers la liaison active.
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("naomy_connections.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    product_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="PENDING", index=True)  # PENDING, SENT, FAILED, SKIPPED
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False,
                                                 index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NaomyReminder(Base):
    __tablename__ = "naomy_reminders"
    __table_args__ = (UniqueConstraint("tenant_id", "reference", name="uq_naomy_reminder_reference"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
                                                 nullable=False, index=True)
    reference: Mapped[str] = mapped_column(String(64), nullable=False)
    sent_on: Mapped[date] = mapped_column(Date, nullable=False)  # jour de la boutique
    sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
