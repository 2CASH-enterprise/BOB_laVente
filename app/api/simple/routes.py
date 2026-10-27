"""Lot 90 — « mode simple » du tableau de bord : mode de l'utilisateur et données des quatre grands boutons."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user
from app.services import simple_mode

router = APIRouter(prefix="/api/v1/simple", tags=["simple"])


class ModeChoice(BaseModel):
    mode: str


async def _context(db, current_user):
    from app.models.tenant import Tenant
    from app.models.user import User

    return await db.get(Tenant, current_user.tenant_id), await db.get(User, current_user.user_id)


@router.get("/mode")
async def get_mode(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tenant, user = await _context(db, current_user)
    return {"mode": simple_mode.effective_mode(user, tenant), "default": simple_mode.default_mode(tenant),
            "suggested": simple_mode.default_mode(tenant) == "SIMPLE"}


@router.put("/mode")
async def set_mode(payload: ModeChoice, current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if payload.mode not in simple_mode.MODES:
        raise HTTPException(status_code=400, detail="Mode inconnu")
    tenant, user = await _context(db, current_user)
    user.ui_mode = payload.mode
    await db.commit()
    return {"mode": user.ui_mode, "default": simple_mode.default_mode(tenant),
            "suggested": simple_mode.default_mode(tenant) == "SIMPLE"}


@router.get("/home")
async def simple_home(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tenant, user = await _context(db, current_user)
    return await simple_mode.home(db, tenant, user)


@router.get("/waiting")
async def simple_waiting(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tenant, _ = await _context(db, current_user)
    return {"items": await simple_mode.waiting_list(db, tenant)}


@router.get("/to-confirm")
async def simple_to_confirm(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tenant, _ = await _context(db, current_user)
    return {"items": await simple_mode.to_confirm_list(db, tenant)}


@router.get("/sales")
async def simple_sales(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tenant, _ = await _context(db, current_user)
    return await simple_mode.sales_summary(db, tenant)
