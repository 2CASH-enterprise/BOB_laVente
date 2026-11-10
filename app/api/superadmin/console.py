"""
Lot 120 — interrupteur entre les Super Admin (contrat 1.5, section « console ») : la liste des consoles, le passage
vers Naomy (jeton de 60 s, usage unique), l'entrée chez Bob depuis Naomy, et la liste de l'équipe pour Naomy.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.rate_limit_dependency import get_rate_limiter
from app.core.security import CurrentSuperAdmin, create_superadmin_access_token, need
from app.models.superadmin_user import SuperAdminUser
from app.services import console_bridge as cb

router = APIRouter(tags=["superadmin"])
REFUSED = "Ce passage a expiré ou n'est pas valable. Reconnectez-vous à la console."


class Leave(BaseModel):
    agent: str


class Enter(BaseModel):
    jeton: str


@router.get("/api/v1/superadmin/console/agents")
async def console_agents(current: CurrentSuperAdmin = Depends(need("overview"))) -> dict:
    return {"agents": cb.agents("bob")}


@router.post("/api/v1/superadmin/console/sortie")
async def console_leave(payload: Leave, current: CurrentSuperAdmin = Depends(need("overview")),
                        db: AsyncSession = Depends(get_db)) -> dict:
    from app.core.config import get_settings
    from app.services.audit import log_audit_event

    url = cb.console_url(payload.agent)
    if url is None:
        raise HTTPException(status_code=404, detail="Cette console n'est pas encore reliée à Bob.")
    member = await db.get(SuperAdminUser, current.superadmin_user_id)
    token = cb.make_token(get_settings().naomy_platform_secret, member, payload.agent)
    await log_audit_event(db, actor=f"superadmin:{member.id}", action="CONSOLE_SWITCH", details={"agent": payload.agent})
    await db.commit()
    return {"url": f"{url}{'&' if '?' in url else '?'}jeton={token}"}


@router.post("/api/v1/superadmin/console/entree")
async def console_enter(payload: Enter, request: Request, db: AsyncSession = Depends(get_db),
                        limiter: RateLimiter = Depends(get_rate_limiter)) -> dict:
    """Public : le jeton signé par Naomy. Bob fait foi : membre retrouvé par e-mail, actif, rôles de Bob."""
    from app.core.config import get_settings
    from app.services.audit import log_audit_event

    ip = request.client.host if request.client else "unknown"
    if not await limiter.is_allowed(f"console-entree:{ip}", 20, 600):
        raise HTTPException(status_code=429, detail="Trop de tentatives, réessayez plus tard.")
    try:
        data = cb.read_token(get_settings().naomy_platform_secret, payload.jeton, "bob")
        if data.get("iss") != "naomy":
            raise cb.ConsoleError("émetteur inconnu")
        email = str(data["membre"].get("email") or "").strip().lower()
        member = (await db.execute(select(SuperAdminUser).where(func.lower(SuperAdminUser.email) == email))).scalar_one_or_none()
        if member is None or not member.active or member.invite_token_hash is not None or not email:
            raise cb.ConsoleError("membre inconnu ou désactivé")
        await cb.spend(db, data)
    except cb.ConsoleError:
        await db.rollback()
        raise HTTPException(status_code=401, detail=REFUSED) from None
    await log_audit_event(db, actor=f"superadmin:{member.id}", action="CONSOLE_ENTRY", ip_address=ip, details={"from": "naomy"})
    await db.commit()
    return {"access_token": create_superadmin_access_token(member.id), "token_type": "bearer"}


@router.get("/api/v1/naomy/console/membres")
async def console_members(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    """Pour Naomy (signature de plateforme sur un corps vide) : toute l'équipe AgenC'AI, telle que Bob la connaît."""
    from app.core.config import get_settings
    from app.services import naomy_bridge as nb

    secret = get_settings().naomy_platform_secret
    if not secret:
        raise nb.NaomyError(503, "indisponible", "La connexion à Naomy n'est pas encore activée sur Bob.")
    if not nb.platform_signature_ok(secret, request.headers.get("x-platform-timestamp"),
                                    request.headers.get("x-platform-signature"), b""):
        raise nb.NaomyError(401, "signature_invalide", "Signature de plateforme invalide.")
    members = (await db.execute(select(SuperAdminUser).order_by(SuperAdminUser.created_at))).scalars().all()
    return {"membres": [cb.member_view(m) for m in members if m.invite_token_hash is None]}
