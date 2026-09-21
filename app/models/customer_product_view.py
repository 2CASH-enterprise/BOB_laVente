import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CustomerProductView(Base):
    """
    Mémoire client au-delà d'une conversation : trace les produits qu'un client a
    consultés (via search_products/recommend_products) sans forcément acheter, pour
    que Bob puisse s'en souvenir lors d'une visite ultérieure, même dans une toute
    nouvelle conversation.
    """

    __tablename__ = "customer_product_views"
    __table_args__ = (UniqueConstraint("tenant_id", "customer_id", "product_id", name="uq_customer_product_view"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False)

    view_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_viewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
