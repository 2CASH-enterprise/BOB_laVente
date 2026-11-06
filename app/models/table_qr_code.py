import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TableQrCode(Base):
    """
    Lot 108 — QR code posé sur une table du restaurant. Il passe toujours par /t/{code} : le client arrive sur WhatsApp
    avec « Je suis à la table 7 », et Bob sait qu'il commande sur place, à cette table (aucun numéro à écrire).
    """

    __tablename__ = "table_qr_codes"
    __table_args__ = (UniqueConstraint("code", name="uq_table_qr_code"),
                      UniqueConstraint("tenant_id", "label", name="uq_table_qr_label"))

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(8), nullable=False)  # « 7 », « T12 », « B3 »
    scan_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
