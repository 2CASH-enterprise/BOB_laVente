"""Lot 37b — compteurs des tâches à faire et appareils qui reçoivent les notifications."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.services import notifications

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=20, max_length=200, pattern=r"^[A-Za-z0-9_\-=]+$")
    auth: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_\-=]+$")


class SubscriptionIn(BaseModel):
    endpoint: str = Field(max_length=1000)
    keys: SubscriptionKeys


class SubscriptionOut(BaseModel):
    endpoint: str = Field(max_length=1000)


@router.get("/counts")
async def get_counts(
    seen: bool = True,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Ce qu'il reste à faire. Les compteurs vus ici ne déclenchent plus de notification.
    Lot 56 : un onglet en arrière-plan demande seen=false — personne ne les a vus, la notification
    du téléphone doit encore partir.
    """
    tenant = await db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Boutique introuvable")
    counts = await notifications.task_counts(db, tenant)
    if not seen:
        return counts
    await notifications.remember_counts(db, tenant.id, counts)
    try:
        await db.commit()
    except IntegrityError:  # deux onglets au tout premier affichage : l'autre a déjà enregistré
        await db.rollback()
    return counts


@router.get("/push-config")
async def push_config(current_user: CurrentUser = Depends(get_current_user)) -> dict:
    settings = get_settings()
    enabled = notifications.push_enabled()
    return {"enabled": enabled, "public_key": settings.vapid_public_key if enabled else None}


@router.post("/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def subscribe(
    payload: SubscriptionIn,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    if not notifications.push_enabled():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Notifications pas encore activées sur le serveur")
    try:
        endpoint = notifications.validate_endpoint(payload.endpoint)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    user = await db.get(User, current_user.user_id)
    if user is None or user.tenant_id != current_user.tenant_id or not user.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Compte non autorisé")
    await notifications.save_subscription(db, user, endpoint, payload.keys.p256dh, payload.keys.auth,
                                          request.headers.get("user-agent"))
    # Point de départ : les tâches déjà là ne déclenchent pas une avalanche de notifications.
    tenant = await db.get(Tenant, user.tenant_id)
    await notifications.remember_counts(db, tenant.id, await notifications.task_counts(db, tenant))
    await db.commit()


@router.post("/subscriptions/delete", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe(
    payload: SubscriptionOut,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Cet appareil ne reçoit plus de notification (désactivation ou déconnexion)."""
    await notifications.forget_device(db, current_user.user_id, payload.endpoint)
    await db.commit()
