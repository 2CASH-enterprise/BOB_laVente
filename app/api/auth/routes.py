from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import create_access_token, hash_password, verify_password
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import RegisterTenantRequest, TenantCreatedResponse, TokenResponse

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/register-tenant", response_model=TenantCreatedResponse, status_code=status.HTTP_201_CREATED)
async def register_tenant(payload: RegisterTenantRequest, db: AsyncSession = Depends(get_db)) -> TenantCreatedResponse:
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
    await db.commit()

    return TenantCreatedResponse(tenant_id=tenant.id, owner_user_id=owner.id)


@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    repo = UserRepository(db)
    user = await repo.get_by_email(form_data.username)  # OAuth2 form utilise "username" pour l'email

    if user is None or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou mot de passe incorrect",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role.value)
    return TokenResponse(access_token=token)
