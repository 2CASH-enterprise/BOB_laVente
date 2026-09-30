import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TenantAppointmentSettings(Base):
    """
    Lot 29 — réservation en ligne des rendez-vous (concession) : horaires d'ouverture, durée d'un
    rendez-vous et nombre de clients reçus en même temps. Bob ne propose que des créneaux calculés
    ici (services/booking.py), jamais inventés.
    """

    __tablename__ = "tenant_appointment_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_appointment_settings_tenant"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    online_booking: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # {"0": [["09:00", "12:30"], ["14:00", "18:00"]], ...} — 0 = lundi ; jour absent = fermé.
    opening_hours: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    slot_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
