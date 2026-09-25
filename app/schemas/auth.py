from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginResponse(BaseModel):
    """
    Deux formes possibles : soit access_token directement (2FA désactivée), soit
    mfa_required + mfa_pending_token (2FA activée, code envoyé, à vérifier ensuite).
    """
    access_token: str | None = None
    token_type: str = "bearer"
    mfa_required: bool = False
    mfa_pending_token: str | None = None


class VerifyMfaRequest(BaseModel):
    mfa_pending_token: str
    code: str = Field(min_length=6, max_length=6)


class ResendMfaRequest(BaseModel):
    mfa_pending_token: str


class MfaToggleRequest(BaseModel):
    enabled: bool


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


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    """Toujours la même réponse, que le compte existe ou non (aucune énumération possible)."""
    message: str = "Si un compte existe pour cet email, un lien de réinitialisation vient d'être envoyé."


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8)  # même règle qu'à l'inscription
