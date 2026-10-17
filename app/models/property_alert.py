"""
Lot 80 — alerte « nouveau bien » envoyée à un acheteur ou un locataire (agence immobilière).

Une ligne par envoi réussi : elle sert à ne jamais prévenir deux fois du même bien au même prix, à limiter le
nombre d'alertes par contact et par semaine, et à compter le plafond mensuel du plan.
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

CHANNEL_WHATSAPP = "WHATSAPP"   # message gratuit : le client a écrit il y a moins de 20 h
CHANNEL_EMAIL = "EMAIL"
CHANNEL_TEMPLATE = "TEMPLATE"   # modèle WhatsApp « marketing », payant : plan Pro seulement
REASON_NEW = "NEW"
REASON_PRICE_DROP = "PRICE_DROP"


class PropertyAlert(Base):
    __tablename__ = "property_alerts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)  # prix du bien au moment de l'alerte
    sent_by: Mapped[str] = mapped_column(String(64), nullable=False)  # identifiant de l'agent, ou « BOB » (automatique)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
