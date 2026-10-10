import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Prospect(Base):
    """
    Lot 61 — prospect d'AgenC'AI (Super Admin → Prospection) : une entreprise à qui l'on propose Bob.

    Chaque prospect a un code unique : son lien personnel /p/CODE (envoyé à la main par WhatsApp, email, SMS…)
    note le clic et dépose le code dans le navigateur (30 jours) ; la démo puis l'inscription faites ensuite
    lui sont rattachées. Étapes : ajouté → contacté → a cliqué → démo → compte créé → activé → payant.
    « Activé » et « payant » se lisent sur la boutique créée (jamais recopiés ici).
    status : ACTIVE (en cours), NOT_INTERESTED, UNSUBSCRIBED (ne plus jamais contacter), INVALID (coordonnées fausses).
    """

    __tablename__ = "prospects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True, index=True)
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    phone: Mapped[str | None] = mapped_column(String(32), index=True)  # normalisé avec l'indicatif (2250707…)
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="CI")
    city: Mapped[str | None] = mapped_column(String(120))
    sector: Mapped[str] = mapped_column(String(32), nullable=False, default="ONLINE_STORE")
    source: Mapped[str | None] = mapped_column(String(120))  # « Salon de l'auto 2026 », « Réseau », « Annuaire »…
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("superadmin_users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE", server_default="ACTIVE", index=True)
    status_reason: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    next_action_on: Mapped[date | None] = mapped_column(Date)

    contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_click_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_click_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    click_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    demo_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    demo_tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"))
    signed_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"), index=True)

    # Lot 62 — emails de prospection : campagne en cours, dernier email envoyé (1 à 3), prochain envoi prévu.
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("prospect_campaigns.id", ondelete="SET NULL"), index=True)
    email_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_email_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    first_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ProspectEvent(Base):
    """Historique d'un prospect : ajout, contact (canal), clic, démo, inscription, changement de statut, note."""

    __tablename__ = "prospect_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prospect_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("prospects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # ADDED, CONTACT, CLICK, DEMO, SIGNUP, STATUS, NOTE
    channel: Mapped[str | None] = mapped_column(String(16))  # EMAIL, PHONE, WHATSAPP, SMS, VISIT, EVENT, OTHER
    detail: Mapped[str | None] = mapped_column(String(500))
    actor: Mapped[str | None] = mapped_column(String(255))  # nom du Super Admin, ou « prospect »
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ProspectCampaign(Base):
    """
    Lot 62 — campagne d'emails de prospection : à qui (secteur, source, pays ; prospects jamais contactés),
    quoi (3 emails au plus : 1er jour, 4e jour, 10e jour), et son état (DRAFT, RUNNING, PAUSED).
    Les textes acceptent {prenom}, {entreprise}, {ville}, {lien}.
    """

    __tablename__ = "prospect_campaigns"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sector: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(2))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT", server_default="DRAFT")
    subject_1: Mapped[str] = mapped_column(String(200), nullable=False)
    body_1: Mapped[str] = mapped_column(Text, nullable=False)
    subject_2: Mapped[str | None] = mapped_column(String(200))
    body_2: Mapped[str | None] = mapped_column(Text)
    subject_3: Mapped[str | None] = mapped_column(String(200))
    body_3: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProspectEmail(Base):
    """Un email de prospection envoyé (ou en échec) : étape, Message-ID (pour reconnaître la réponse), ouverture."""

    __tablename__ = "prospect_emails"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prospect_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("prospects.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("prospect_campaigns.id", ondelete="SET NULL"), index=True)
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # SENT, FAILED, BOUNCED
    message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    error: Mapped[str | None] = mapped_column(String(300))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)


class ProspectSuppression(Base):
    """Adresses à ne plus jamais contacter (désinscription, adresse inexistante) : seulement une empreinte."""

    __tablename__ = "prospect_suppressions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)  # UNSUBSCRIBED, BOUNCED
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProspectionSettings(Base):
    """Réglages de l'envoi (une seule ligne) : plafond par jour, dernier message lu dans la boîte, dernière erreur."""

    __tablename__ = "prospection_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=20, server_default="20")
    sending_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    last_imap_uid: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(300))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
