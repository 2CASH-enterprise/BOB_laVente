"""
Points de contact traçables : un lien WhatsApp à coller partout (Facebook, catalogue,
Instagram, TikTok…) et, pour les sites web, une bulle (widget) qui utilise ce même lien.

Endpoints publics (aucune authentification) :
- GET /w/{code}           : compte le clic et redirige vers WhatsApp (message pré-rempli) ;
- GET /w/{code}/config    : réglages d'affichage du widget (aucune donnée sensible) ;
- GET /widget.js          : le script à coller sur un site.
"""
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.contact_point import ContactPoint
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.whatsapp_account import WhatsAppAccount
from app.schemas.contact_point import ContactPointCreate, ContactPointLimits, ContactPointResponse, ContactPointUpdate
from app.services.plan_limits import can_create_contact_point, count_contact_points, max_contact_points

router = APIRouter(tags=["contact_points"])

CODE_LENGTH = 8
_WIDGET_JS = Path(__file__).resolve().parents[2] / "static" / "widget" / "widget.js"


def _generate_code() -> str:
    return secrets.token_urlsafe(8)[:CODE_LENGTH]


def _to_response(cp: ContactPoint, customer_count: int) -> ContactPointResponse:
    return ContactPointResponse(
        id=cp.id, code=cp.code, name=cp.name, greeting=cp.greeting, position=cp.position,
        active=cp.active, click_count=cp.click_count, customer_count=customer_count,
        short_path=f"/w/{cp.code}", created_at=cp.created_at,
    )


async def _customer_counts(db: AsyncSession, tenant_id) -> dict:
    stmt = (
        select(Customer.acquisition_contact_point_id, func.count(Customer.id))
        .where(Customer.tenant_id == tenant_id, Customer.acquisition_contact_point_id.is_not(None))
        .group_by(Customer.acquisition_contact_point_id)
    )
    return dict((await db.execute(stmt)).all())


async def _get_owned(db: AsyncSession, tenant_id, contact_point_id: UUID) -> ContactPoint:
    stmt = select(ContactPoint).where(
        ContactPoint.tenant_id == tenant_id, ContactPoint.id == contact_point_id, ContactPoint.archived_at.is_(None)
    )
    cp = (await db.execute(stmt)).scalar_one_or_none()
    if cp is None:
        raise HTTPException(status_code=404, detail="Point de contact introuvable")
    return cp


# ---------------------------------------------------------------------------
# Gestion (dashboard)
# ---------------------------------------------------------------------------

@router.get("/api/v1/contact-points", response_model=list[ContactPointResponse])
async def list_contact_points(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(ContactPoint)
        .where(ContactPoint.tenant_id == current_user.tenant_id, ContactPoint.archived_at.is_(None))
        .order_by(ContactPoint.created_at)
    )
    points = (await db.execute(stmt)).scalars().all()
    counts = await _customer_counts(db, current_user.tenant_id)
    return [_to_response(cp, counts.get(cp.id, 0)) for cp in points]


@router.get("/api/v1/contact-points/limits", response_model=ContactPointLimits)
async def contact_point_limits(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant = await db.get(Tenant, current_user.tenant_id)
    return ContactPointLimits(used=await count_contact_points(db, tenant.id), max=max_contact_points(tenant))


@router.post(
    "/api/v1/contact-points", response_model=ContactPointResponse, status_code=201,
    dependencies=[Depends(require_role("MANAGER"))],
)
async def create_contact_point(
    payload: ContactPointCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant = await db.get(Tenant, current_user.tenant_id)
    if not await can_create_contact_point(db, tenant):
        limit = max_contact_points(tenant)
        hint = " Passez à un plan payant pour en créer jusqu'à 10." if not tenant.is_paid else ""
        raise HTTPException(status_code=403, detail=f"Limite de {limit} lien(s) atteinte pour votre plan.{hint}")

    for _ in range(5):  # collision de code quasi impossible, mais jamais silencieuse
        code = _generate_code()
        exists = (await db.execute(select(ContactPoint.id).where(ContactPoint.code == code))).first()
        if not exists:
            break
    else:
        raise HTTPException(status_code=500, detail="Impossible de générer un code unique, réessayez")

    cp = ContactPoint(
        tenant_id=tenant.id, code=code, name=payload.name.strip(), greeting=payload.greeting.strip(),
        position=payload.position,
    )
    db.add(cp)
    await db.commit()
    await db.refresh(cp)
    return _to_response(cp, 0)


@router.patch(
    "/api/v1/contact-points/{contact_point_id}", response_model=ContactPointResponse,
    dependencies=[Depends(require_role("MANAGER"))],
)
async def update_contact_point(
    contact_point_id: UUID,
    payload: ContactPointUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cp = await _get_owned(db, current_user.tenant_id, contact_point_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        setattr(cp, field, value.strip() if isinstance(value, str) else value)
    await db.commit()
    await db.refresh(cp)
    counts = await _customer_counts(db, current_user.tenant_id)
    return _to_response(cp, counts.get(cp.id, 0))


@router.delete(
    "/api/v1/contact-points/{contact_point_id}", status_code=204,
    dependencies=[Depends(require_role("MANAGER"))],
)
async def archive_contact_point(
    contact_point_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Suppression douce : le lien cesse de fonctionner, les clients rattachés restent intacts."""
    cp = await _get_owned(db, current_user.tenant_id, contact_point_id)
    cp.active = False
    cp.archived_at = datetime.now(timezone.utc)
    await db.commit()


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

_INACTIVE_PAGE = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Lien inactif</title>
<style>body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f4f5f7;
color:#1f2933;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}
.card{background:#fff;max-width:420px;padding:32px 28px;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,.06);text-align:center}
h1{font-size:20px;margin:0 0 12px}p{font-size:15px;line-height:1.5;color:#52606d;margin:0}</style></head>
<body><div class="card"><h1>Ce lien n'est plus actif</h1>
<p>Le commerce a peut-être changé de lien. Contactez-le directement par ses autres moyens habituels.</p></div></body></html>"""


async def _active_contact_point(db: AsyncSession, code: str) -> ContactPoint | None:
    stmt = select(ContactPoint).where(
        ContactPoint.code == code, ContactPoint.active.is_(True), ContactPoint.archived_at.is_(None)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


@router.get("/w/{code}/config")
async def widget_config(code: str, db: AsyncSession = Depends(get_db)):
    """
    Lu par widget.js à chaque affichage (cache court) : le commerçant peut modifier ou
    désactiver sans que le site partenaire ne recolle son code. Aucune donnée sensible.
    """
    headers = {"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"}
    cp = await _active_contact_point(db, code)
    if cp is None:
        return JSONResponse({"active": False}, status_code=404, headers=headers)
    tenant = await db.get(Tenant, cp.tenant_id)
    return JSONResponse(
        {"active": True, "position": cp.position, "branding": not tenant.is_paid},
        headers=headers,
    )


@router.get("/w/{code}")
async def contact_point_redirect(
    code: str,
    p: str | None = Query(default=None, max_length=64, description="SKU d'un produit du catalogue"),
    db: AsyncSession = Depends(get_db),
):
    cp = await _active_contact_point(db, code)
    if cp is None:
        return HTMLResponse(_INACTIVE_PAGE, status_code=404)

    account_stmt = select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == cp.tenant_id)
    account = (await db.execute(account_stmt)).scalar_one_or_none()
    if account is None or not account.display_phone_number:
        return HTMLResponse(_INACTIVE_PAGE, status_code=404)

    message = cp.greeting
    if p:
        # Le produit est TOUJOURS lu dans le catalogue de CE commerce : un site partenaire
        # ne peut ni faire écrire n'importe quoi au client, ni pointer le produit d'un autre.
        product_stmt = select(Product).where(
            Product.tenant_id == cp.tenant_id, Product.sku == p, Product.active.is_(True)
        )
        product = (await db.execute(product_stmt)).scalar_one_or_none()
        if product is not None:
            message = f"Bonjour, je suis intéressé(e) par : {product.name}"

    # Incrément atomique (plusieurs clics simultanés ne se perdent pas).
    await db.execute(update(ContactPoint).where(ContactPoint.id == cp.id).values(click_count=ContactPoint.click_count + 1))
    await db.commit()

    digits = "".join(ch for ch in account.display_phone_number if ch.isdigit())
    # [W:code] : attribution au premier contact, retirée par le webhook avant tout traitement.
    text = quote(f"{message} [W:{cp.code}]")
    return RedirectResponse(url=f"https://wa.me/{digits}?text={text}", status_code=302)


@router.get("/widget.js")
async def widget_script():
    return FileResponse(
        _WIDGET_JS,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"},
    )
