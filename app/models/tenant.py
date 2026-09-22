import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CompanySize(StrEnum):
    """Section 57.4"""

    INDEPENDANT = "INDEPENDANT"
    PETITE_PME = "PETITE_PME"
    MOYENNE_PME = "MOYENNE_PME"
    ENTERPRISE = "ENTERPRISE"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)  # ISO 3166-1 alpha-2
    currency: Mapped[str] = mapped_column(String(3), nullable=False)  # ISO 4217
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    website_url: Mapped[str | None] = mapped_column(String(500))
    is_demo: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_paid: Mapped[bool] = mapped_column(default=False, nullable=False)  # False = freemium

    # Section 57 — segmentation
    company_size: Mapped[CompanySize] = mapped_column(
        Enum(CompanySize, name="company_size"), default=CompanySize.INDEPENDANT, nullable=False
    )
    employee_count: Mapped[int | None]
    annual_revenue_band: Mapped[str | None] = mapped_column(String(64))
    plan_id: Mapped[str | None] = mapped_column(String(64))
    plan_assigned_by: Mapped[str] = mapped_column(String(64), default="AUTO")

    active: Mapped[bool] = mapped_column(default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
