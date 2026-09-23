from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.rate_limit_dependency import get_rate_limiter
from app.core.security import (
    CurrentUser,
    create_access_token,
    create_mfa_pending_token,
    decode_mfa_pending_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import (
    LoginResponse,
    MfaToggleRequest,
    RegisterTenantRequest,
    ResendMfaRequest,
    TenantCreatedResponse,
    TokenResponse,
    VerifyMfaRequest,
)
from app.services.audit import log_audit_event
from app.services.email_service import send_email
from app.services.otp_service import generate_otp, hash_otp, verify_otp

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

LOGIN_RATE_LIMIT = 5  # tentatives
LOGIN_RATE_WINDOW_SECONDS = 60


@router.post("/register-tenant", response_model=TenantCreatedResponse, status_code=status.HTTP_201_CREATED)
async def register_tenant(
    payload: RegisterTenantRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> TenantCreatedResponse:
    """
    Section 6 — Ajouter une entreprise.
    Crée le tenant et son premier utilisateur, avec le rôle OWNER.
    """
    repo = UserRepository(db)
    existing = await repo.get_by_email(payload.owner_email)
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cet email est déjà utilisé")

    tenant = Tenant(
        name=payload.company_name,
        country=payload.country.upper(),
        currency=payload.currency.upper(),
        phone=payload.phone,
        email=payload.owner_email,
    )
    db.add(tenant)
    await db.flush()  # obtient tenant.id sans committer

    owner = User(
        tenant_id=tenant.id,
        email=payload.owner_email,
        hashed_password=hash_password(payload.owner_password),
        full_name=payload.owner_full_name,
        role=Role.OWNER,
    )
    db.add(owner)
    await db.flush()  # nécessaire pour obtenir owner.id avant de journaliser

    await log_audit_event(
        db,
        actor=str(owner.id),
        action="TENANT_REGISTERED",
        tenant_id=tenant.id,
        details={"company_name": payload.company_name, "owner_email": payload.owner_email},
        ip_address=request.client.host if request.client else None,
    )

    await db.commit()

    return TenantCreatedResponse(tenant_id=tenant.id, owner_user_id=owner.id)


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
) -> LoginResponse:
    client_ip = request.client.host if request.client else "unknown"

    # Section 32 — protection brute-force : limite par IP ET par email visé, pour bloquer
    # à la fois un attaquant qui varie les emails et un attaquant qui varie les IP.
    allowed_ip = await rate_limiter.is_allowed(f"login:ip:{client_ip}", limit=20, window_seconds=60)
    allowed_email = await rate_limiter.is_allowed(f"login:email:{form_data.username}", limit=LOGIN_RATE_LIMIT, window_seconds=LOGIN_RATE_WINDOW_SECONDS)
    if not allowed_ip or not allowed_email:
        await log_audit_event(db, actor="ANONYMOUS", action="LOGIN_RATE_LIMITED", ip_address=client_ip, details={"email": form_data.username})
        await db.commit()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Trop de tentatives, réessayez plus tard")

    repo = UserRepository(db)
    user = await repo.get_by_email(form_data.username)  # OAuth2 form utilise "username" pour l'email

    if user is None or not verify_password(form_data.password, user.hashed_password):
        await log_audit_event(
            db, actor="ANONYMOUS", action="LOGIN_FAILED", ip_address=client_ip, details={"email": form_data.username}
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou mot de passe incorrect",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.mfa_enabled:
        await _send_mfa_code(db, user)
        await log_audit_event(db, actor=str(user.id), action="LOGIN_MFA_CODE_SENT", tenant_id=user.tenant_id, ip_address=client_ip)
        await db.commit()
        return LoginResponse(mfa_required=True, mfa_pending_token=create_mfa_pending_token(user.id))

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role.value)
    await log_audit_event(db, actor=str(user.id), action="LOGIN_SUCCESS", tenant_id=user.tenant_id, ip_address=client_ip)
    await db.commit()

    return LoginResponse(access_token=token)


async def _send_mfa_code(db: AsyncSession, user: User) -> None:
    code = generate_otp()
    user.mfa_otp_code_hash = hash_otp(code)
    user.mfa_otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    send_email(
        to=user.email,
        subject="Votre code de connexion Bob",
        body=f"Votre code de vérification est : {code}\n\nIl expire dans 10 minutes. Si vous n'êtes pas à l'origine de cette tentative de connexion, ignorez cet email.",
    )


@router.post("/verify-mfa", response_model=TokenResponse)
async def verify_mfa(payload: VerifyMfaRequest, request: Request, db: AsyncSession = Depends(get_db), rate_limiter: RateLimiter = Depends(get_rate_limiter)) -> TokenResponse:
    user_id = decode_mfa_pending_token(payload.mfa_pending_token)
    client_ip = request.client.host if request.client else "unknown"

    # Un code à 6 chiffres a peu de combinaisons : limiter strictement les tentatives.
    allowed = await rate_limiter.is_allowed(f"mfa:verify:{user_id}", limit=5, window_seconds=300)
    if not allowed:
        await db.commit()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Trop de tentatives, redemandez un code")

    user = await db.get(User, user_id)
    if user is None or user.mfa_otp_code_hash is None or user.mfa_otp_expires_at is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Aucune vérification en attente")

    expires_at = user.mfa_otp_expires_at
    if expires_at.tzinfo is None:  # SQLite (tests) renvoie parfois une valeur naïve
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Code expiré, redemandez-en un")

    if not verify_otp(payload.code, user.mfa_otp_code_hash):
        await log_audit_event(db, actor=str(user.id), action="LOGIN_MFA_CODE_INVALID", tenant_id=user.tenant_id, ip_address=client_ip)
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Code incorrect")

    user.mfa_otp_code_hash = None
    user.mfa_otp_expires_at = None

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role.value)
    await log_audit_event(db, actor=str(user.id), action="LOGIN_SUCCESS", tenant_id=user.tenant_id, ip_address=client_ip, details={"mfa": True})
    await db.commit()

    return TokenResponse(access_token=token)


@router.post("/mfa/resend", status_code=status.HTTP_204_NO_CONTENT)
async def resend_mfa(payload: ResendMfaRequest, db: AsyncSession = Depends(get_db), rate_limiter: RateLimiter = Depends(get_rate_limiter)) -> None:
    user_id = decode_mfa_pending_token(payload.mfa_pending_token)
    allowed = await rate_limiter.is_allowed(f"mfa:resend:{user_id}", limit=3, window_seconds=300)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Trop de demandes, patientez")

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session invalide")
    await _send_mfa_code(db, user)
    await db.commit()


@router.put("/mfa/toggle", response_model=TokenResponse)
async def toggle_mfa(
    payload: MfaToggleRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Réutilise le même token (aucune réauthentification exigée pour ce MVP — voir section 2FA)."""
    user = await db.get(User, current_user.user_id)
    user.mfa_enabled = payload.enabled
    if not payload.enabled:
        user.mfa_otp_code_hash = None
        user.mfa_otp_expires_at = None
    await db.commit()
    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role.value)
    return TokenResponse(access_token=token)


@router.get("/me")
async def get_me(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    user = await db.get(User, current_user.user_id)
    return {"id": str(user.id), "email": user.email, "full_name": user.full_name, "role": user.role.value, "mfa_enabled": user.mfa_enabled}
