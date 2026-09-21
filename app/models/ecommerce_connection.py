import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EcommercePlatform(StrEnum):
    SHOPIFY = "SHOPIFY"
    META_CATALOG = "META_CATALOG"
    # WOOCOMMERCE, PRESTASHOP à ajouter ultérieurement, même modèle.


class SyncStatus(StrEnum):
    NEVER_SYNCED = "NEVER_SYNCED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class EcommerceConnection(Base):
    """Section 25, niveau 2 — synchronisation catalogue via API externe."""

    __tablename__ = "ecommerce_connections"
    __table_args__ = (UniqueConstraint("tenant_id", "platform", name="uq_ecommerce_tenant_platform"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[EcommercePlatform] = mapped_column(Enum(EcommercePlatform, name="ecommerce_platform"), nullable=False)

    # Identifiant de la connexion selon la plateforme : domaine boutique pour Shopify,
    # ID de catalogue pour Meta Commerce Catalog. Nom conservé pour éviter une migration
    # de renommage ; la sémantique dépend de `platform`.
    shop_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    access_token: Mapped[str] = mapped_column(String(2048), nullable=False)  # chiffré au repos (section 32)
    currency: Mapped[str | None] = mapped_column(String(3))

    auto_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus, name="sync_status"), default=SyncStatus.NEVER_SYNCED
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
