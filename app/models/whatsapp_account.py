import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DisplayNameStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class BusinessVerificationStatus(StrEnum):
    NOT_SUBMITTED = "NOT_SUBMITTED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    FAILED = "FAILED"


class VerificationSubmittedBy(StrEnum):
    SELF = "SELF"
    PLATFORM = "PLATFORM"


class WhatsAppAccount(Base):
    """Section 59.4 / 59.8 — un WABA par tenant, jamais partagé (section 10)."""

    __tablename__ = "whatsapp_accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )

    waba_id: Mapped[str] = mapped_column(String(64), nullable=False)
    phone_number_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    business_id: Mapped[str | None] = mapped_column(String(64))

    display_name: Mapped[str | None] = mapped_column(String(255))
    display_name_status: Mapped[DisplayNameStatus] = mapped_column(
        Enum(DisplayNameStatus, name="display_name_status"), default=DisplayNameStatus.PENDING
    )

    system_user_token: Mapped[str] = mapped_column(String(2048), nullable=False)  # chiffré au repos (section 32)
    coexistence_mode: Mapped[bool] = mapped_column(Boolean, default=False)

    verification_status: Mapped[str | None] = mapped_column(String(32))
    business_verification_status: Mapped[BusinessVerificationStatus] = mapped_column(
        Enum(BusinessVerificationStatus, name="business_verification_status"),
        default=BusinessVerificationStatus.NOT_SUBMITTED,
    )
    business_verification_submitted_by: Mapped[VerificationSubmittedBy | None] = mapped_column(
        Enum(VerificationSubmittedBy, name="verification_submitted_by")
    )

    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
