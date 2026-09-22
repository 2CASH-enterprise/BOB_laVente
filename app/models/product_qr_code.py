import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ProductQrCode(Base):
    """
    QR code vers un produit vedette : redirige toujours via /qr/{code} (jamais un lien
    wa.me codé en dur), pour que le commerçant puisse changer le produit ciblé sans
    jamais réimprimer le QR physique, et pour compter les scans réels.
    """

    __tablename__ = "product_qr_codes"
    __table_args__ = (UniqueConstraint("code", name="uq_product_qr_code"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )

    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    scan_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
