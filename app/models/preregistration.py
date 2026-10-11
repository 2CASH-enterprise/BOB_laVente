"""
Lot 65 — pré-inscription : interrupteur de la plateforme (une seule ligne) et fichiers déposés par les pré-inscrits
pour préparer l'installation (produits, clients). Les fichiers restent en base, jamais dans un dossier public.
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PlatformSettings(Base):
    __tablename__ = "platform_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    prereg_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    prereg_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prereg_changed_by: Mapped[str | None] = mapped_column(String(120))
    # Lot 68 — modèles WhatsApp : rien n'est créé ni envoyé chez Meta tant que le Super Admin ne l'allume pas.
    wa_templates_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Lot 69 — places de l'offre de lancement (les N premiers comptes pré-inscrits).
    launch_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    # Lot 69 — plafonds de conversations par formule (vide = valeurs par défaut de plan_quota) et conduite au dépassement :
    # WARN = Bob continue et on prévient (par défaut) ; PAUSE = Bob en pause jusqu'au mois suivant.
    plan_quotas: Mapped[dict | None] = mapped_column(JSON)
    quota_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="WARN", server_default="WARN")


class PreregistrationFile(Base):
    __tablename__ = "preregistration_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # PRODUCTS (ou contrats, véhicules), CUSTOMERS
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
