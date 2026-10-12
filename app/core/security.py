"""
Sécurité : hachage des mots de passe, émission/vérification JWT,
dépendances FastAPI pour l'utilisateur courant et l'isolation multi-tenant (section 30).

Principe critique (section 30) : toute route métier doit dépendre de `get_current_tenant_id`
plutôt que de faire confiance à un tenant_id fourni dans l'URL ou le corps de la requête.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.database import get_db

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


class TokenPayload(BaseModel):
    sub: str  # user_id
    tenant_id: str
    role: str
    exp: datetime


def create_access_token(user_id: UUID, tenant_id: UUID, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


# ==================== 2FA — token temporaire « en attente de code » ====================
# Volontairement différent d'un TokenPayload normal : ne contient ni rôle ni droits,
# ne peut jamais servir à s'authentifier sur une route protégée par get_current_user.

class MfaPendingTokenPayload(BaseModel):
    sub: str  # user_id
    mfa_pending: bool
    exp: datetime


def create_mfa_pending_token(user_id: UUID) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=10)
    payload = {"sub": str(user_id), "mfa_pending": True, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_mfa_pending_token(token: str) -> UUID:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        parsed = MfaPendingTokenPayload(**payload)
    except (JWTError, ValidationError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session de vérification invalide ou expirée") from exc
    if not parsed.mfa_pending:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Jeton invalide")
    return UUID(parsed.sub)


def decode_access_token(token: str) -> TokenPayload:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        return TokenPayload(**payload)
    except (JWTError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Jeton invalide ou expiré",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


class CurrentUser(BaseModel):
    user_id: UUID
    tenant_id: UUID
    role: str


async def get_current_user(request: Request, token: str = Depends(oauth2_scheme), db=Depends(get_db)) -> CurrentUser:
    payload = decode_access_token(token)
    user = CurrentUser(user_id=UUID(payload.sub), tenant_id=UUID(payload.tenant_id), role=payload.role)
    # Lot 67 — boutique concernée si une erreur survient pendant cette requête (monitoring).
    request.state.monitor_tenant = user.tenant_id
    from app.services.monitoring import current_tenant

    current_tenant.set(user.tenant_id)
    await _check_account(request, db, user)
    return user


async def _check_account(request: Request, db, user: CurrentUser) -> None:
    """
    Lot 70 — le compte doit encore exister, être actif et appartenir à cette boutique : un utilisateur supprimé ou
    désactivé perd l'accès tout de suite (avant : jusqu'à l'expiration de son jeton, 60 minutes).
    Lot 65 — un compte pré-inscrit (accès pas encore ouvert) ne peut appeler que l'accueil d'exemple, sa
    pré-inscription et la connexion : tout le reste répond 403, quel que soit le chemin pris par la page.
    Une seule requête pour les deux contrôles.
    """
    from sqlalchemy import select

    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services import preregistration

    row = (await db.execute(
        select(User.active, Tenant.prereg_at, Tenant.access_opened_at)
        .join(Tenant, Tenant.id == User.tenant_id)
        .where(User.id == user.user_id, User.tenant_id == user.tenant_id)
    )).first()
    if row is None or row.active is False:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Compte désactivé ou introuvable",
                            headers={"WWW-Authenticate": "Bearer"})
    if row.prereg_at is None or row.access_opened_at is not None:
        return
    if not preregistration.allowed(request.method, request.url.path):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=preregistration.LOCKED_MESSAGE)


def get_current_tenant_id(current_user: CurrentUser = Depends(get_current_user)) -> UUID:
    """
    Point d'entrée UNIQUE pour obtenir le tenant_id côté backend (section 30).
    Toute requête aux repositories doit utiliser cette valeur, jamais un tenant_id
    passé librement par le client dans l'URL ou le body.
    """
    return current_user.tenant_id


# RBAC (section 31)
ROLE_HIERARCHY = {"VIEWER": 0, "AGENT": 1, "MANAGER": 2, "ADMIN": 3, "OWNER": 4}


def require_role(minimum_role: str):
    """Dependency factory : refuse la requête si le rôle de l'utilisateur est insuffisant."""

    def _check(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if ROLE_HIERARCHY.get(current_user.role, -1) < ROLE_HIERARCHY.get(minimum_role, 99):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Rôle insuffisant : {minimum_role} requis",
            )
        return current_user

    return _check


# ==================== Super Admin (Étape 3) ====================
# Système d'authentification VOLONTAIREMENT séparé de get_current_user/CurrentUser
# ci-dessus : un super admin n'appartient à aucun tenant, et ne doit jamais pouvoir
# être confondu avec un utilisateur tenant même en cas de bug ailleurs dans le code.

superadmin_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/superadmin/login", auto_error=False)


class SuperAdminTokenPayload(BaseModel):
    sub: str  # superadmin_user_id
    superadmin: bool
    exp: datetime


def create_superadmin_access_token(user_id: UUID) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": str(user_id), "superadmin": True, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


# Lot 70 — jeton « code Super Admin en attente » : ni `superadmin` ni `mfa_pending` (celui des commerçants) dedans,
# il ne peut donc ouvrir AUCUNE route ; il sert seulement à envoyer le code à 6 chiffres.
class SuperAdminMfaPendingPayload(BaseModel):
    sub: str
    sa_mfa_pending: bool
    exp: datetime


def create_superadmin_mfa_pending_token(user_id: UUID) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=10)
    return jwt.encode({"sub": str(user_id), "sa_mfa_pending": True, "exp": expire}, settings.secret_key,
                      algorithm=settings.algorithm)


def decode_superadmin_mfa_pending_token(token: str) -> UUID:
    try:
        parsed = SuperAdminMfaPendingPayload(**jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm]))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Vérification expirée, reconnectez-vous") from exc
    if not parsed.sa_mfa_pending:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Jeton invalide")
    return UUID(parsed.sub)


class CurrentSuperAdmin(BaseModel):
    superadmin_user_id: UUID


async def get_current_superadmin(token: str | None = Depends(superadmin_oauth2_scheme), db=Depends(get_db)) -> CurrentSuperAdmin:
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Non authentifié")
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        parsed = SuperAdminTokenPayload(**payload)
    except Exception as exc:  # noqa: BLE001 — couvre à la fois JWTError et une validation pydantic ratée
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Jeton invalide ou expiré") from exc
    if not parsed.superadmin:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Jeton invalide")
    # Lot 70 — un compte Super Admin désactivé (ou effacé) perd l'accès tout de suite.
    from sqlalchemy import select

    from app.models.superadmin_user import SuperAdminUser

    active = (await db.execute(select(SuperAdminUser.active).where(SuperAdminUser.id == UUID(parsed.sub)))).scalar_one_or_none()
    if not active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Compte désactivé ou introuvable")
    return CurrentSuperAdmin(superadmin_user_id=UUID(parsed.sub))
