"""Lot 121 — « Mon équipe » : le choix Bob seul / Bob + Naomy, et « Rencontrer Naomy » en un clic."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.services import team

router = APIRouter(prefix="/api/v1/team", tags=["team"])


class Choice(BaseModel):
    mode: str


class Meet(BaseModel):
    confirmer_essai: bool = False
    request_id: str | None = None


async def _tenant(db, current_user):
    from app.models.tenant import Tenant

    return await db.get(Tenant, current_user.tenant_id)


@router.get("")
async def team_view(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    return await team.view(db, await _tenant(db, current_user))


@router.put("", dependencies=[Depends(require_role("ADMIN"))])
async def team_choose(payload: Choice, current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)) -> dict:
    try:
        return await team.choose(db, await _tenant(db, current_user), payload.mode)
    except team.TeamError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None


@router.post("/naomy", dependencies=[Depends(require_role("ADMIN"))])
async def team_meet_naomy(payload: Meet | None = None, current_user: CurrentUser = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)) -> dict:
    """409 `essai_a_confirmer` : la formule gratuite demande d'abord l'accord pour l'essai équipe de 7 jours."""
    payload = payload or Meet()
    tenant = await _tenant(db, current_user)
    try:
        link = await team.meet_naomy(db, tenant, payload.confirmer_essai, payload.request_id)
    except team.TeamError as exc:
        await db.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    return {**link, "team": await team.view(db, tenant)}
