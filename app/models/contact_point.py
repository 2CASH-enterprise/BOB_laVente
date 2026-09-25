import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

POSITION_LEFT = "LEFT"
POSITION_RIGHT = "RIGHT"


class ContactPoint(Base):
    """
    Point de contact traçable (lien WhatsApp + widget pour sites web).

    Un même point de contact s'utilise comme lien (bio/publication Facebook, catalogue,
    Instagram, TikTok…) ou comme bulle sur un site web. Le clic passe TOUJOURS par
    /w/{code} (jamais un lien wa.me direct) : le numéro du commerçant n'est jamais exposé,
    chaque clic est compté, et le commerçant peut modifier ou désactiver sans que le
    partenaire ne touche à son code.

    Jamais supprimé physiquement (archivé) : des clients y restent rattachés.
    """

    __tablename__ = "contact_points"
    __table_args__ = (UniqueConstraint("code", name="uq_contact_point_code"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)  # ex. « Page Facebook », « Mon site »
    greeting: Mapped[str] = mapped_column(String(300), nullable=False)  # message pré-rempli par défaut
    position: Mapped[str] = mapped_column(String(8), nullable=False, default=POSITION_RIGHT)  # bulle du widget

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    click_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
