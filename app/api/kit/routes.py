"""Lot 59 — kit de lancement : lien suivi, QR code, affiche et textes pour faire écrire les clients sur WhatsApp."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.tenant import Tenant
from app.services import launch_kit
from app.services.audit import log_audit_event

router = APIRouter(prefix="/api/v1/kit", tags=["kit"])


class KitIn(BaseModel):
    greeting: str = Field(..., max_length=launch_kit.GREETING_MAX)


async def _kit(db, user: CurrentUser) -> tuple[Tenant, dict]:
    tenant = await db.get(Tenant, user.tenant_id)
    data = await launch_kit.kit(db, tenant, get_settings().public_base_url)
    await db.commit()  # le lien du kit est créé au premier affichage
    return tenant, data


@router.get("")
async def get_kit(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    return (await _kit(db, current_user))[1]


@router.put("", dependencies=[Depends(require_role("MANAGER"))])
async def update_kit(payload: KitIn, current_user: CurrentUser = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)) -> dict:
    """Le message pré-rempli quand le client ouvre le lien ou scanne le QR code."""
    greeting = " ".join(payload.greeting.split())
    if len(greeting) < 2:
        raise HTTPException(status_code=422, detail="Indiquez le message pré-rempli")
    tenant = await db.get(Tenant, current_user.tenant_id)
    cp, _ = await launch_kit.kit_link(db, tenant)
    if cp is None:
        raise HTTPException(status_code=409, detail="Connectez d'abord votre numéro WhatsApp (Intégrations)")
    cp.greeting = greeting
    await log_audit_event(db, actor=str(current_user.user_id), action="KIT_GREETING_UPDATED", tenant_id=tenant.id, details={})
    await db.commit()
    return (await _kit(db, current_user))[1]


@router.get("/poster", response_class=HTMLResponse)
async def kit_poster(model: str = "agence", accent: str = "or", photo: UUID | None = None,
                     current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    """Lot 103 — affiche vitrine premium (agence A4, sombre A4, chevalet A5) avec le QR code du kit."""
    from app.models.product import Product
    from app.services import vitrine_posters

    tenant, data = await _kit(db, current_user)
    if not data["connected"]:
        raise HTTPException(status_code=409, detail="Connectez d'abord votre numéro WhatsApp (Intégrations)")
    picture = None
    if photo is not None:
        product = await db.get(Product, photo)
        if product is None or product.tenant_id != tenant.id:
            raise HTTPException(status_code=404, detail="Photo introuvable")
        picture = vitrine_posters.photo_of(product)
    try:
        page = vitrine_posters.kit_poster(tenant, data, model, accent, picture)
    except vitrine_posters.PosterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/poster-options")
async def kit_poster_options(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Modèles, teintes et photos possibles (les produits ou biens actifs qui ont une photo)."""
    from sqlalchemy import select

    from app.models.product import Product
    from app.services import vitrine_posters as vp

    rows = (await db.execute(select(Product).where(Product.tenant_id == current_user.tenant_id, Product.active.is_(True))
                             .order_by(Product.created_at.desc()).limit(200))).scalars().all()
    photos = [{"id": str(p.id), "name": p.name, "url": vp.photo_of(p)} for p in rows if vp.photo_of(p)][:60]
    return {"models": [{"code": m, "label": vp.MODEL_LABELS[m]} for m in vp.KIT_MODELS],
            "accents": [{"code": c, "label": vp.ACCENT_LABELS[c], "color": vp.ACCENTS[c][0]} for c in vp.ACCENTS],
            "photos": photos}
