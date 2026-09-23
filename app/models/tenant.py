import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TenantPlan(StrEnum):
    """
    Grille de plans définitive (fige toute segmentation précédente — company_size,
    plan_id, is_paid). FREE = freemium ; les 4 autres = payant.
    """

    FREE = "FREE"
    INDEPENDANT = "INDEPENDANT"
    STARTER = "STARTER"
    PRO = "PRO"
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
    company_profile: Mapped[str | None] = mapped_column(Text)  # présentation libre — injectée dans le prompt de l'agent
    is_demo: Mapped[bool] = mapped_column(default=False, nullable=False)

    plan: Mapped[TenantPlan] = mapped_column(Enum(TenantPlan, name="tenant_plan"), default=TenantPlan.FREE, nullable=False)
    plan_assigned_by: Mapped[str] = mapped_column(String(64), default="AUTO")

    active: Mapped[bool] = mapped_column(default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def is_paid(self) -> bool:
        """Calculé depuis `plan`, jamais stocké séparément — plus aucune double source de vérité."""
        return self.plan != TenantPlan.FREE
