"""
Lot 65 — pré-inscription.

- Public : le mode est-il allumé (boutons « Je me pré-inscris » de la présentation, de la démo, de l'inscription) ?
- Compte pré-inscrit : sa fenêtre (contenu selon le secteur, offre), « Mes informations », fichiers à préparer.
- Super Admin : interrupteur, liste des pré-inscrits, ouverture de l'accès (un compte ou tous), fichiers.
"""
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentSuperAdmin, CurrentUser, get_current_superadmin, get_current_user, require_role
from app.models.preregistration import PreregistrationFile
from app.models.superadmin_user import SuperAdminUser
from app.models.tenant import Tenant
from app.models.user import User
from app.services import preregistration as pre
from app.services.audit import log_audit_event
from app.services.business_type import BUSINESS_TYPES, normalize

public_router = APIRouter(prefix="/api/v1/preregistration", tags=["preregistration"])
router = APIRouter(prefix="/api/v1/preregistration/me", tags=["preregistration"])
admin_router = APIRouter(prefix="/api/v1/superadmin/preregistration", tags=["preregistration"])


@public_router.get("/public")
async def public_state(db: AsyncSession = Depends(get_db)) -> dict:
    s = await pre.slots(db)  # lot 69 : compteur honnête des places de l'offre de lancement
    return {"mode": await pre.mode_on(db), "offer": pre.OFFER_TEXT, "slots_total": s["total"], "slots_left": s["left"],
            "offer_open": s["left"] > 0}


# --- Le compte pré-inscrit ---------------------------------------------------------------------------

class PrepIn(BaseModel):
    whatsapp: str | None = Field(None, max_length=40)
    catalog_size: str | None = Field(None, max_length=16)
    note: str | None = Field(None, max_length=500)


async def _tenant(db, user: CurrentUser) -> Tenant:
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None or tenant.prereg_at is None:
        raise HTTPException(status_code=404, detail="Ce compte n'est pas pré-inscrit")
    return tenant


async def _me_view(db, tenant, user: CurrentUser) -> dict:
    person = await db.get(User, user.user_id)
    first = (person.full_name or "").split()[0] if person and (person.full_name or "").strip() else None
    return {
        "locked": pre.is_locked(tenant), "prereg_at": tenant.prereg_at.isoformat(),
        "access_opened_at": tenant.access_opened_at.isoformat() if tenant.access_opened_at else None,
        "company": tenant.name, "first_name": first, "sector": normalize(tenant.business_type),
        "offer": pre.OFFERS.get(tenant.launch_offer or ""),
        # Lot 69 — inscrit après la dernière place : la fenêtre ne promet pas l'offre.
        "content": {**pre.content(tenant.business_type), **({} if tenant.launch_offer else {"offer": None})},
        "sizes": pre.CATALOG_SIZES, "prep": pre.prep_view(tenant, await pre.files_of(db, tenant.id)),
    }


@router.get("")
async def my_preregistration(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    return await _me_view(db, await _tenant(db, current_user), current_user)


@router.put("/prep", dependencies=[Depends(require_role("ADMIN"))])
async def save_prep(payload: PrepIn, current_user: CurrentUser = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await _tenant(db, current_user)
    try:
        tenant.prep_whatsapp = pre.clean_phone(payload.whatsapp)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if payload.catalog_size and payload.catalog_size not in pre.CATALOG_SIZES:
        raise HTTPException(status_code=422, detail="Choisissez une taille dans la liste")
    tenant.prep_catalog_size = payload.catalog_size or None
    tenant.prep_note = " ".join((payload.note or "").split()) or None
    tenant.prep_updated_at = pre.now_utc()
    await db.commit()
    return await _me_view(db, tenant, current_user)


@router.post("/files", dependencies=[Depends(require_role("ADMIN"))])
async def upload_file(kind: str = Form(...), file: UploadFile = File(...), current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await _tenant(db, current_user)
    data = await file.read(pre.FILE_MAX_BYTES + 1)
    try:
        await pre.save_file(db, tenant, kind, file.filename or "", file.content_type, data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit_event(db, actor=str(current_user.user_id), action="PREREG_FILE_UPLOADED", tenant_id=tenant.id,
                          details={"kind": kind, "size": len(data)})
    await db.commit()
    return await _me_view(db, tenant, current_user)


@router.delete("/files/{file_id}", dependencies=[Depends(require_role("ADMIN"))])
async def delete_file(file_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await _tenant(db, current_user)
    record = await db.get(PreregistrationFile, file_id)
    if record is None or record.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Fichier introuvable")
    await db.delete(record)
    await db.commit()
    return await _me_view(db, tenant, current_user)


# --- Super Admin -------------------------------------------------------------------------------------

class ModeIn(BaseModel):
    mode: bool


async def _admin(db, admin: CurrentSuperAdmin) -> SuperAdminUser:
    me = await db.get(SuperAdminUser, admin.superadmin_user_id)
    if me is None or not me.active:
        raise HTTPException(status_code=401, detail="Non authentifié")
    return me


async def _overview(db) -> dict:
    row = await pre.settings_row(db)
    tenants = (await db.execute(select(Tenant).where(Tenant.prereg_at.is_not(None))
                                .order_by(Tenant.prereg_at.desc()))).scalars().all()
    files: dict = {}
    if tenants:
        for f in (await db.execute(select(PreregistrationFile).where(
                PreregistrationFile.tenant_id.in_([t.id for t in tenants])).order_by(PreregistrationFile.uploaded_at))).scalars():
            files.setdefault(f.tenant_id, []).append(f)
    rows = []
    for t in tenants:
        owner = await pre.owner_of(db, t)
        rows.append({
            "id": str(t.id), "name": t.name, "sector": normalize(t.business_type),
            "sector_label": BUSINESS_TYPES[normalize(t.business_type)]["label"], "country": t.country,
            "email": owner.email if owner else t.email, "owner": owner.full_name if owner else None, "phone": t.phone,
            "prereg_at": t.prereg_at.isoformat(), "access_opened_at": t.access_opened_at.isoformat() if t.access_opened_at else None,
            "locked": pre.is_locked(t), "offer": t.launch_offer, "prep": pre.prep_view(t, files.get(t.id, [])),
        })
    return {"mode": row.prereg_mode, "changed_at": row.prereg_changed_at.isoformat() if row.prereg_changed_at else None,
            "changed_by": row.prereg_changed_by, "offer": pre.OFFER_TEXT, "slots": await pre.slots(db),
            "counts": {"locked": sum(1 for r in rows if r["locked"]), "opened": sum(1 for r in rows if not r["locked"])},
            "tenants": rows}


@admin_router.get("")
async def admin_overview(admin: CurrentSuperAdmin = Depends(get_current_superadmin), db: AsyncSession = Depends(get_db)) -> dict:
    await _admin(db, admin)
    data = await _overview(db)
    await db.commit()
    return data


@admin_router.put("/settings")
async def admin_mode(payload: ModeIn, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                     db: AsyncSession = Depends(get_db)) -> dict:
    me = await _admin(db, admin)
    row = await pre.settings_row(db)
    if row.prereg_mode != payload.mode:
        row.prereg_mode = payload.mode
        row.prereg_changed_at = datetime.now(timezone.utc)
        row.prereg_changed_by = me.full_name or me.email
        await log_audit_event(db, actor=f"superadmin:{me.id}", action="PREREG_MODE_CHANGED", details={"mode": payload.mode})
    await db.commit()
    return await _overview(db)


class SlotsIn(BaseModel):
    total: int = Field(..., ge=0, le=10000)


@admin_router.put("/slots")
async def admin_slots(payload: SlotsIn, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                      db: AsyncSession = Depends(get_db)) -> dict:
    """Lot 69 — nombre de places de l'offre de lancement (100 par défaut)."""
    me = await _admin(db, admin)
    row = await pre.settings_row(db)
    if row.launch_slots != payload.total:
        await log_audit_event(db, actor=f"superadmin:{me.id}", action="LAUNCH_SLOTS_CHANGED",
                              details={"from": row.launch_slots, "to": payload.total})
        row.launch_slots = payload.total
    await db.commit()
    return await _overview(db)


async def _prereg_tenant(db, tenant_id: UUID) -> Tenant:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or tenant.prereg_at is None:
        raise HTTPException(status_code=404, detail="Pré-inscrit introuvable")
    return tenant


@admin_router.post("/tenants/{tenant_id}/open")
async def admin_open(tenant_id: UUID, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                     db: AsyncSession = Depends(get_db)) -> dict:
    me = await _admin(db, admin)
    tenant = await _prereg_tenant(db, tenant_id)
    if await pre.open_access(db, tenant):
        await log_audit_event(db, actor=f"superadmin:{me.id}", action="PREREG_ACCESS_OPENED", tenant_id=tenant.id)
    await db.commit()
    return await _overview(db)


@admin_router.post("/tenants/{tenant_id}/relock")
async def admin_relock(tenant_id: UUID, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                       db: AsyncSession = Depends(get_db)) -> dict:
    """Remet un compte en pré-inscription (bêta arrêtée) : il ne voit plus que l'accueil d'exemple."""
    me = await _admin(db, admin)
    tenant = await _prereg_tenant(db, tenant_id)
    if tenant.access_opened_at is not None:
        tenant.access_opened_at = None
        await log_audit_event(db, actor=f"superadmin:{me.id}", action="PREREG_ACCESS_CLOSED", tenant_id=tenant.id)
    await db.commit()
    return await _overview(db)


@admin_router.post("/open-all")
async def admin_open_all(admin: CurrentSuperAdmin = Depends(get_current_superadmin), db: AsyncSession = Depends(get_db)) -> dict:
    """Jour du lancement : l'accès complet pour tous les pré-inscrits (un email chacun)."""
    me = await _admin(db, admin)
    tenants = (await db.execute(select(Tenant).where(Tenant.prereg_at.is_not(None),
                                                     Tenant.access_opened_at.is_(None)))).scalars().all()
    opened = 0
    for tenant in tenants:
        if await pre.open_access(db, tenant):
            opened += 1
    await log_audit_event(db, actor=f"superadmin:{me.id}", action="PREREG_ACCESS_OPENED_ALL", details={"opened": opened})
    await db.commit()
    return {**await _overview(db), "opened": opened}


@admin_router.get("/files/{file_id}")
async def admin_file(file_id: UUID, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                     db: AsyncSession = Depends(get_db)) -> Response:
    await _admin(db, admin)
    record = await db.get(PreregistrationFile, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Fichier introuvable")
    safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in record.filename)
    return Response(record.data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"', "Cache-Control": "no-store"})
