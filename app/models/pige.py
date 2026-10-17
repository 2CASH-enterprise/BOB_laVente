"""
Lot 81 — assistant de pige (agence immobilière) et ventes réelles DVF pour le prix de comparaison.

- PigeListing : une annonce de particulier collée par l'agent. Données personnelles réduites au nécessaire (nom et
  téléphone du vendeur s'ils figurent dans l'annonce) ; effacées à la demande (« Ne plus contacter » : seule une empreinte
  du numéro reste, pour ne jamais le rappeler) et après 6 mois sans activité.
- DvfSale / DvfDepartment : ventes réelles publiées par l'État (Demandes de valeurs foncières, licence ouverte),
  chargées seulement pour les départements où les agences piges.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid, func
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PigeListing(Base):
    __tablename__ = "pige_listings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(500))
    site: Mapped[str | None] = mapped_column(String(40))
    text: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(String(200))
    transaction: Mapped[str | None] = mapped_column(String(16))     # VENTE, LOCATION
    property_type: Mapped[str | None] = mapped_column(String(16))   # APPARTEMENT, MAISON… (types du lot 79)
    city: Mapped[str | None] = mapped_column(String(100))
    postal_code: Mapped[str | None] = mapped_column(String(5))
    surface: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    rooms: Mapped[int | None] = mapped_column(Integer)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    dpe: Mapped[str | None] = mapped_column(String(1))
    owner_name: Mapped[str | None] = mapped_column(String(100))
    owner_phone: Mapped[str | None] = mapped_column(String(30))
    phone_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    no_agency: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false(), nullable=False)
    online_days: Mapped[int | None] = mapped_column(Integer)
    price_dropped: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false(), nullable=False)
    extraction: Mapped[str] = mapped_column(String(8), default="RULES", server_default="RULES", nullable=False)
    analysis: Mapped[dict | None] = mapped_column(JSON)              # score, raisons, prix DVF, acheteurs
    status: Mapped[str] = mapped_column(String(16), default="A_APPELER", server_default="A_APPELER", nullable=False)
    next_call_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DvfSale(Base):
    __tablename__ = "dvf_sales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    department: Mapped[str] = mapped_column(String(3), nullable=False, index=True)
    postal_code: Mapped[str | None] = mapped_column(String(5), index=True)
    commune: Mapped[str | None] = mapped_column(String(100))
    sold_on: Mapped[date] = mapped_column(Date, nullable=False)
    property_type: Mapped[str] = mapped_column(String(16), nullable=False)   # APPARTEMENT, MAISON
    surface: Mapped[int] = mapped_column(Integer, nullable=False)
    rooms: Mapped[int | None] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class DvfDepartment(Base):
    __tablename__ = "dvf_departments"

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    loaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sales: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    years: Mapped[str | None] = mapped_column(String(60))
    error: Mapped[str | None] = mapped_column(String(300))
