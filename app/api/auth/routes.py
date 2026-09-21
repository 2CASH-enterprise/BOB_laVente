from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.rate_limit_dependency import get_rate_limiter
from app.core.security import create_access_token, hash_password, verify_password
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import RegisterTenantRequest, TenantCreatedResponse, TokenResponse
from app.services.audit import log_audit_event

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


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
) -> TokenResponse:
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

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role.value)
    await log_audit_event(db, actor=str(user.id), action="LOGIN_SUCCESS", tenant_id=user.tenant_id, ip_address=client_ip)
    await db.commit()

    return TokenResponse(access_token=token)
