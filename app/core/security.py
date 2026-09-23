"""
Sécurité : hachage des mots de passe, émission/vérification JWT,
dépendances FastAPI pour l'utilisateur courant et l'isolation multi-tenant (section 30).

Principe critique (section 30) : toute route métier doit dépendre de `get_current_tenant_id`
plutôt que de faire confiance à un tenant_id fourni dans l'URL ou le corps de la requête.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings

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


def get_current_user(token: str = Depends(oauth2_scheme)) -> CurrentUser:
    payload = decode_access_token(token)
    return CurrentUser(user_id=UUID(payload.sub), tenant_id=UUID(payload.tenant_id), role=payload.role)


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


class CurrentSuperAdmin(BaseModel):
    superadmin_user_id: UUID


def get_current_superadmin(token: str | None = Depends(superadmin_oauth2_scheme)) -> CurrentSuperAdmin:
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Non authentifié")
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        parsed = SuperAdminTokenPayload(**payload)
    except Exception as exc:  # noqa: BLE001 — couvre à la fois JWTError et une validation pydantic ratée
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Jeton invalide ou expiré") from exc
    if not parsed.superadmin:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Jeton invalide")
    return CurrentSuperAdmin(superadmin_user_id=UUID(parsed.sub))
