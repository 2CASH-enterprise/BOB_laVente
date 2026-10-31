"""
Lot 95 — panier du client sur WhatsApp (parcours à boutons des boutiques).

Un panier par client et par boutique, gardé 24 h après la dernière action. Les lignes ne gardent que le produit, la
variante (taille ou modèle) et la quantité : le prix et le stock sont toujours relus dans le catalogue, jamais
mémorisés ici. « awaiting » note ce que Bob attend d'un message écrit (une quantité, une adresse).
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Cart(Base):
    __tablename__ = "carts"
    __table_args__ = (UniqueConstraint("tenant_id", "customer_id", name="uq_carts_tenant_customer"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"),
                                                   nullable=False)
    # [{"product_id": "…", "variant": 2 | None, "label": "Taille 42" | None, "quantity": 1}]
    items: Mapped[list | None] = mapped_column(JSON)
    fulfillment: Mapped[str | None] = mapped_column(String(16))  # DELIVERY ou PICKUP, choisi par le client
    address: Mapped[str | None] = mapped_column(String(500))
    awaiting: Mapped[str | None] = mapped_column(String(120))  # « qty:<produit>:<variante> », « address »
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False,
                                                 index=True)
