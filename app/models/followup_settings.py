import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Lot 49 — les relances partent par EMAIL (jamais sur WhatsApp) : ces textes ouvrent l'email.
DEFAULT_FIRST_MESSAGE = ("Nous n'avons pas eu de nouvelles depuis notre dernier échange. Avez-vous trouvé ce que vous "
                         "cherchiez ? Nous restons à votre disposition sur WhatsApp pour répondre à vos questions.")
DEFAULT_SECOND_MESSAGE = ("Petit rappel : votre demande nous tient à cœur. Écrivez-nous sur WhatsApp, nous vous "
                          "répondons tout de suite.")


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
    # Lot 49 — offre du moment, écrite par le commerçant (jamais inventée par Bob) : remise, soldes, jeu…
    offer_text: Mapped[str | None] = mapped_column(Text)
    offer_code: Mapped[str | None] = mapped_column(String(40))
    offer_ends_on: Mapped[date | None] = mapped_column(Date)  # au-delà, l'offre n'est plus citée

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
