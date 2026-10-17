"""
Lot 81 — assistant de pige (agence immobilière). Réservé à l'agence (verrou côté serveur), toujours limité aux annonces
de SA boutique ; plafond du plan et options Pro vérifiés ici. Bob ne contacte jamais le particulier.
"""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.pige import DvfDepartment, PigeListing
from app.models.tenant import Tenant
from app.services import pige
from app.services.audit import log_audit_event
from app.services.business_type import only_real_estate

router = APIRouter(prefix="/api/v1/pige", tags=["pige"], dependencies=[Depends(only_real_estate())])


class ListingIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    url: str | None = Field(default=None, max_length=500)
    online_days: int | None = Field(default=None, ge=0, le=3650)
    price_dropped: bool = False


class ListingUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    transaction: str | None = None
    property_type: str | None = None
    city: str | None = Field(default=None, max_length=100)
    postal_code: str | None = Field(default=None, max_length=5)
    surface: Decimal | None = None
    rooms: int | None = None
    price: Decimal | None = None
    dpe: str | None = Field(default=None, max_length=1)
    owner_name: str | None = Field(default=None, max_length=100)
    owner_phone: str | None = Field(default=None, max_length=30)
    online_days: int | None = Field(default=None, ge=0, le=3650)
    price_dropped: bool | None = None
    no_agency: bool | None = None
    status: str | None = None
    next_call_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)


async def _tenant(db, current_user) -> Tenant:
    tenant = await db.get(Tenant, current_user.tenant_id)
    if not pige.available(tenant):
        raise HTTPException(status_code=403, detail=pige.NOT_AVAILABLE)
    return tenant


def _num(value):
    return float(value) if value is not None else None


def _out(listing: PigeListing) -> dict:
    from app.services.product_sheets import PROPERTY_CATEGORIES

    analysis = listing.analysis or {}
    return {
        "id": str(listing.id), "title": listing.title, "site": listing.site, "source_url": listing.source_url,
        "transaction": listing.transaction, "transaction_label": pige.TRANSACTIONS.get(listing.transaction),
        "property_type": listing.property_type,
        "property_label": PROPERTY_CATEGORIES.get(listing.property_type, {}).get("label"),
        "city": listing.city, "postal_code": listing.postal_code, "surface": _num(listing.surface), "rooms": listing.rooms,
        "price": _num(listing.price), "dpe": listing.dpe, "owner_name": listing.owner_name, "owner_phone": listing.owner_phone,
        "no_agency": listing.no_agency, "online_days": listing.online_days, "price_dropped": listing.price_dropped,
        "extraction": listing.extraction, "status": listing.status, "status_label": pige.STATUSES.get(listing.status),
        "next_call_at": listing.next_call_at.isoformat() if listing.next_call_at else None, "notes": listing.notes,
        "text": listing.text, "created_at": listing.created_at.isoformat() if listing.created_at else None,
        "analysis": {**analysis, "priority_label": pige.PRIORITIES.get(analysis.get("priority"))},
    }


@router.get("/settings")
async def settings(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    from app.core.config import get_settings

    tenant = await db.get(Tenant, current_user.tenant_id)
    if not pige.available(tenant):
        return {"available": False, "message": pige.NOT_AVAILABLE}
    config = get_settings()
    departments = (await db.execute(select(DvfDepartment).order_by(DvfDepartment.code))).scalars().all()
    return {"available": True, "pro": pige.is_pro(tenant), "quota": pige.monthly_quota(tenant),
            "used": await pige.used_this_month(db, tenant),
            "ai": pige.is_pro(tenant) and config.llm_provider == "mistral" and bool(config.mistral_api_key),
            "dvf": [{"code": d.code, "sales": d.sales, "years": d.years, "error": d.error,
                     "loaded_at": d.loaded_at.isoformat() if d.loaded_at else None} for d in departments]}


@router.get("")
async def list_listings(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    tenant = await _tenant(db, current_user)
    rows = (await db.execute(select(PigeListing).where(PigeListing.tenant_id == tenant.id))).scalars().all()
    end_of_day = datetime.now(timezone.utc).replace(hour=23, minute=59)

    def order(listing):
        due = listing.next_call_at is not None and (listing.next_call_at if listing.next_call_at.tzinfo
                                                     else listing.next_call_at.replace(tzinfo=timezone.utc)) <= end_of_day
        closed = listing.status in ("OPPOSITION", "PAS_INTERESSE", "MANDAT")
        return (closed, not due, -((listing.analysis or {}).get("points") or 0), -(listing.created_at.timestamp() if listing.created_at else 0))

    return [_out(listing) for listing in sorted(rows, key=order)]


@router.post("", status_code=201, dependencies=[Depends(require_role("MANAGER"))])
async def add_listing(payload: ListingIn, current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)) -> dict:
    from app.agents.orchestrator import record_usage
    from app.core.config import get_settings

    tenant = await _tenant(db, current_user)
    text = payload.text.strip()
    if len(text) < 30:
        raise HTTPException(status_code=422, detail="Collez le texte complet de l'annonce (titre, description, prix).")
    left = await pige.quota_left(db, tenant)
    if left is not None and left <= 0:
        raise HTTPException(status_code=403, detail=f"Plafond du mois atteint ({pige.monthly_quota(tenant)} annonces). "
                                                    "Le plan Pro n'a pas de plafond.")
    url = (payload.url or "").strip() or None
    raw = pige.extract(text, url)
    fields = pige.clean_fields(raw)
    extraction = "RULES"
    if pige.is_pro(tenant):  # Pro : lecture plus complète par l'IA (le numéro du vendeur ne lui est jamais envoyé)
        ai, usage = await pige.ai_extract(text)
        if ai:
            ai_fields = pige.clean_fields({k: v for k, v in ai.items() if k not in ("owner_phone", "owner_name")})
            fields = {**fields, **ai_fields}
            extraction = "AI"
        if usage:
            record_usage(db, tenant.id, None, "PIGE", get_settings().mistral_classifier_model, {"usage": usage})
    hashed = pige.phone_hash(tenant.id, fields.get("owner_phone"))
    if await pige.suppressed(db, tenant.id, hashed):
        raise HTTPException(status_code=409, detail="Ce vendeur a demandé à ne plus être contacté par votre agence.")
    if await pige.duplicate_of(db, tenant.id, url, hashed) is not None:
        raise HTTPException(status_code=409, detail="Cette annonce est déjà dans votre pige.")
    listing = PigeListing(tenant_id=tenant.id, created_by=str(current_user.user_id), source_url=url, site=raw.get("site"),
                          text=text[:pige.TEXT_MAX], phone_hash=hashed, extraction=extraction,
                          online_days=payload.online_days, price_dropped=payload.price_dropped,
                          last_activity_at=datetime.now(timezone.utc), **fields)
    db.add(listing)
    await db.flush()
    await pige.analyze(db, tenant, listing)
    await db.commit()
    await db.refresh(listing)
    return _out(listing)


async def _listing(db, tenant, listing_id: UUID) -> PigeListing:
    listing = await db.get(PigeListing, listing_id)
    if listing is None or listing.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Annonce introuvable")
    return listing


@router.put("/{listing_id}", dependencies=[Depends(require_role("MANAGER"))])
async def update_listing(listing_id: UUID, payload: ListingUpdate, current_user: CurrentUser = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await _tenant(db, current_user)
    listing = await _listing(db, tenant, listing_id)
    sent = payload.model_dump(exclude_unset=True)
    if "status" in sent and sent["status"] not in pige.STATUSES:
        raise HTTPException(status_code=422, detail="Statut inconnu")
    data_keys = ("title", "transaction", "property_type", "city", "postal_code", "surface", "rooms", "price", "dpe",
                 "owner_name", "owner_phone")
    cleaned = pige.clean_fields({k: v for k, v in sent.items() if k in data_keys and v is not None})
    for key in data_keys:
        if key in sent:
            setattr(listing, key, cleaned.get(key))  # vide ou douteux : effacé plutôt que gardé faux
    if "owner_phone" in sent:
        listing.phone_hash = pige.phone_hash(tenant.id, listing.owner_phone)
    for key in ("online_days", "price_dropped", "no_agency", "next_call_at", "status"):
        if key in sent and (sent[key] is not None or key in ("online_days", "next_call_at")):
            setattr(listing, key, sent[key])
    if "notes" in sent:
        listing.notes = (sent["notes"] or "").strip() or None
    if listing.status == "OPPOSITION":
        pige.forget(listing)
    listing.last_activity_at = datetime.now(timezone.utc)
    await pige.analyze(db, tenant, listing)
    if sent.get("status") == "OPPOSITION":
        await log_audit_event(db, actor=str(current_user.user_id), action="PIGE_OPPOSITION", tenant_id=tenant.id,
                              details={"listing_id": str(listing.id)})
    await db.commit()
    await db.refresh(listing)
    return _out(listing)


@router.get("/{listing_id}/script")
async def call_script(listing_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)) -> dict:
    from app.models.user import User

    tenant = await _tenant(db, current_user)
    listing = await _listing(db, tenant, listing_id)
    user = await db.get(User, current_user.user_id)
    first = ((user.full_name or "").split() or [None])[0] if user else None
    return pige.script(tenant, listing, first)


@router.post("/{listing_id}/refresh", dependencies=[Depends(require_role("MANAGER"))])
async def refresh(listing_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await _tenant(db, current_user)
    listing = await _listing(db, tenant, listing_id)
    await pige.analyze(db, tenant, listing)
    await db.commit()
    return _out(listing)


@router.delete("/{listing_id}", status_code=204, dependencies=[Depends(require_role("MANAGER"))])
async def delete_listing(listing_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)) -> None:
    tenant = await _tenant(db, current_user)
    listing = await _listing(db, tenant, listing_id)
    if listing.status == "OPPOSITION":
        raise HTTPException(status_code=409, detail="Une opposition est gardée pour ne jamais rappeler ce vendeur.")
    await db.delete(listing)
    await db.commit()
