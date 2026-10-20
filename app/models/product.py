import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, Uuid, event, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Product(Base):
    """Section 4 (schéma products) et 14-17 (outils recherche/stock/prix)."""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sku", name="uq_products_tenant_sku"),
        UniqueConstraint("tenant_id", "external_source", "external_id", name="uq_products_tenant_external"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    cost_price: Mapped[float | None] = mapped_column(Numeric(14, 2))  # optionnel — permet le calcul de marge
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("categories.id"))
    image_url: Mapped[str | None] = mapped_column(String(1000))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Lot 26 — caractéristiques d'un véhicule (concession) : clés et valeurs normalisées par
    # app/services/vehicle.py (année, kilométrage, carburant, boîte…). NULL pour un produit ordinaire.
    vehicle: Mapped[dict | None] = mapped_column(JSON)

    # Lot 78 — commerce : catégorie Bob fixe (services/product_sheets.py), fiche de la catégorie (champs nettoyés)
    # et variantes avec LEUR stock (Shopify : [{"options": {"Taille": "42"}, "stock": 2, "sku": …, "price": …}]).
    bob_category: Mapped[str | None] = mapped_column(String(24))
    sheet: Mapped[dict | None] = mapped_column(JSON)
    variants: Mapped[list | None] = mapped_column(JSON)
    # Lot 78 B — valeurs de la fiche et des variantes à plat (« coton | Nike | M | Rouge »), pour que la recherche
    # trouve un produit par sa matière, sa marque ou sa taille. Tenu à jour automatiquement (voir plus bas).
    sheet_text: Mapped[str | None] = mapped_column(Text)

    # Lot 87 — Bob retouche les photos : image_url est ce que les cartes envoient (studio, montage ou photo d'origine).
    image_original_url: Mapped[str | None] = mapped_column(String(1000))  # la photo du commerçant, toujours gardée
    image_studio_url: Mapped[str | None] = mapped_column(String(1000))    # fond blanc studio + signature
    image_status: Mapped[str | None] = mapped_column(String(16))          # PENDING, DONE, FAILED, ORIGINAL
    worn_status: Mapped[str | None] = mapped_column(String(16))           # PENDING, REVIEW, ACCEPTED, REFUSED, FAILED
    worn_preview_url: Mapped[str | None] = mapped_column(String(1000))    # photo portée à valider
    worn_image_url: Mapped[str | None] = mapped_column(String(1000))      # photo portée acceptée
    card_style: Mapped[str | None] = mapped_column(String(16))            # MONTAGE (réelle | portée) ou WORN

    # Traçabilité d'origine (section 25) — NULL pour un produit saisi manuellement ou importé en CSV.
    external_source: Mapped[str | None] = mapped_column(String(32))  # ex. "SHOPIFY"
    external_id: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def sheet_completeness(self) -> int | None:
        from app.services.product_sheets import completeness

        return completeness(self)


@event.listens_for(Product, "before_insert")
@event.listens_for(Product, "before_update")
def _refresh_sheet_text(mapper, connection, target) -> None:
    """Lot 78 B — quel que soit le chemin (tableau de bord, import, Shopify, Meta), le texte de recherche suit la fiche."""
    from app.services.product_sheets import flatten

    target.sheet_text = flatten(target.sheet, target.variants)
