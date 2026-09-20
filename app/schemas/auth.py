from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RegisterTenantRequest(BaseModel):
    """Section 6 — création d'une nouvelle entreprise + son premier utilisateur OWNER."""

    company_name: str = Field(min_length=1, max_length=255)
    country: str = Field(min_length=2, max_length=2)
    currency: str = Field(min_length=3, max_length=3)
    phone: str | None = None
    owner_email: EmailStr
    owner_full_name: str = Field(min_length=1, max_length=255)
    owner_password: str = Field(min_length=8)


class TenantCreatedResponse(BaseModel):
    tenant_id: UUID
    owner_user_id: UUID
