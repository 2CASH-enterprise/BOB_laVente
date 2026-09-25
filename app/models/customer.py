import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Customer(Base):
    """Section 10 — le numéro WhatsApp identifie le client. Jamais partagé entre deux tenants."""

    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("tenant_id", "whatsapp_number", name="uq_customers_tenant_whatsapp"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    whatsapp_number: Mapped[str] = mapped_column(String(32), nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(500))
    city: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(2))
    language: Mapped[str] = mapped_column(String(5), default="fr")

    # CRM (section CRM) — jamais un simple champ texte libre pour la source : traçable
    # depuis un vrai mécanisme (QR, import...), jamais une supposition de l'IA.
    acquisition_source: Mapped[str | None] = mapped_column(String(64))  # ex. "QR", "IMPORT", "ORGANIC"
    acquisition_detail: Mapped[str | None] = mapped_column(String(255))  # ex. nom du produit scanné
    # Point de contact (lien/widget) qui a amené ce client — vraie référence, pas un texte :
    # les statistiques restent justes même si le point de contact est renommé.
    acquisition_contact_point_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contact_points.id", ondelete="SET NULL"), index=True
    )
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)  # note libre, éditable par un humain (section CRM.9)

    # Consentement marketing — DÉLIBÉRÉMENT séparé de tout statut commercial (section CRM) :
    # avoir déjà acheté ne donne jamais automatiquement le droit d'envoyer une campagne.
    # given_at/source/withdrawn_at : un simple booléen ne suffit pas à prouver un consentement réel.
    marketing_consent: Mapped[bool] = mapped_column(default=False, nullable=False)
    marketing_consent_given_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    marketing_consent_source: Mapped[str | None] = mapped_column(String(32))  # "AI_ASKED", "MANUAL", "IMPORT"
    marketing_consent_withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    marketing_consent_withdrawn_source: Mapped[str | None] = mapped_column(String(32))  # "EMAIL_LINK", "WHATSAPP_KEYWORD", "AI_DETECTED", "MANUAL"

    # Extraction structurée par l'IA (section CRM.3) — clé-valeur flexible plutôt que des
    # colonnes rigides (marque/budget n'ont pas de sens identique en mode/électronique/etc).
    # Toujours une INFÉRENCE de l'IA, jamais un fait vérifié — à afficher comme tel.
    detected_preferences: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
