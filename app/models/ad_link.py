import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AdLink(Base):
    """
    Lot 82 — lien de publication d'AgenC'AI (Super Admin → Prospection) : un lien par publication, publicité ou groupe
    (Facebook, Instagram, LinkedIn…). Il ouvre la démo du secteur (?src=CODE) ; on compte les navigateurs qui l'ont
    ouvert (une fois chacun, jamais les aperçus de lien), puis les démos et comptes créés ensuite (code gardé 30 jours
    dans le navigateur, cookie propre à Bob). Aucun pixel ni cookie publicitaire.
    """

    __tablename__ = "ad_links"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(48), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False, default="FACEBOOK_POST")
    sector: Mapped[str | None] = mapped_column(String(32))
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_click_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AdLinkEvent(Base):
    """Démo (DEMO) ou compte (SIGNUP) venu d'un lien de publication ; une seule fois par boutique."""

    __tablename__ = "ad_link_events"
    __table_args__ = (UniqueConstraint("link_id", "kind", "tenant_id", name="uq_ad_link_event"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    link_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("ad_links.id", ondelete="CASCADE"),
                                               nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    # Lot 73 : une démo effacée après 30 jours garde son comptage (tenant_id devient vide).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"),
                                                        nullable=True, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
