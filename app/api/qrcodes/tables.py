"""
Lot 108 — QR codes des tables d'un restaurant.

Le restaurant crée ses tables d'un coup (« de 1 à 20 ») ; chaque QR imprimé encode /t/{code}, jamais un lien wa.me
direct. Le scan ouvre WhatsApp avec « Bonjour, je suis à la table 7 [T:code] » : la référence est retirée par le
webhook, Bob sait que le client est sur place, à la table 7, et « Commander » mène tout droit à la carte.
"""
import secrets
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.table_qr_code import TableQrCode
from app.models.whatsapp_account import WhatsAppAccount
from app.services.business_type import RESTAURANT, require_business

router = APIRouter(tags=["qrcodes"])

MAX_TABLES = 200  # un grand restaurant ; au-delà, une autre organisation (une ligne par salle)
MAX_PER_CALL = 100
TABLE_TEXT = "Bonjour, je suis à la table {label}"


class TableRange(BaseModel):
    first: int = Field(ge=1, le=999)
    last: int = Field(ge=1, le=999)


def _code() -> str:
    return secrets.token_urlsafe(6)[:8]


def _sort_key(qr: TableQrCode):
    return (0, int(qr.label), "") if qr.label.isdigit() else (1, 0, qr.label)


def _out(qr: TableQrCode) -> dict:
    return {"id": str(qr.id), "code": qr.code, "label": qr.label, "scan_count": qr.scan_count, "short_path": f"/t/{qr.code}"}


async def _tables(db, tenant_id) -> list[TableQrCode]:
    rows = (await db.execute(select(TableQrCode).where(TableQrCode.tenant_id == tenant_id))).scalars().all()
    return sorted(rows, key=_sort_key)


@router.get("/api/v1/table-qr-codes", dependencies=[Depends(require_business(RESTAURANT))])
async def list_tables(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list:
    return [_out(qr) for qr in await _tables(db, current_user.tenant_id)]


@router.post("/api/v1/table-qr-codes", status_code=201,
             dependencies=[Depends(require_business(RESTAURANT)), Depends(require_role("MANAGER"))])
async def create_tables(payload: TableRange, current_user: CurrentUser = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)) -> list:
    """Crée les tables manquantes de la plage (une table qui existe déjà garde son QR : rien à réimprimer)."""
    from app.services.audit import log_audit_event

    if payload.last < payload.first:
        raise HTTPException(status_code=422, detail="Le dernier numéro doit être plus grand que le premier")
    if payload.last - payload.first + 1 > MAX_PER_CALL:
        raise HTTPException(status_code=422, detail=f"{MAX_PER_CALL} tables au plus à la fois")
    existing = await _tables(db, current_user.tenant_id)
    labels = {qr.label for qr in existing}
    wanted = [str(n) for n in range(payload.first, payload.last + 1) if str(n) not in labels]
    if len(existing) + len(wanted) > MAX_TABLES:
        raise HTTPException(status_code=422, detail=f"{MAX_TABLES} tables au plus par restaurant")
    for label in wanted:
        db.add(TableQrCode(tenant_id=current_user.tenant_id, code=_code(), label=label, scan_count=0))
    await log_audit_event(db, actor=str(current_user.user_id), action="TABLE_QR_CREATED", tenant_id=current_user.tenant_id,
                          details={"first": payload.first, "last": payload.last, "created": len(wanted)})
    await db.commit()
    return [_out(qr) for qr in await _tables(db, current_user.tenant_id)]


@router.delete("/api/v1/table-qr-codes/{qr_id}", status_code=204,
               dependencies=[Depends(require_business(RESTAURANT)), Depends(require_role("MANAGER"))])
async def delete_table(qr_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)) -> None:
    qr = (await db.execute(select(TableQrCode).where(TableQrCode.tenant_id == current_user.tenant_id,
                                                     TableQrCode.id == qr_id))).scalar_one_or_none()
    if qr is None:
        raise HTTPException(status_code=404, detail="Table introuvable")
    await db.delete(qr)
    await db.commit()


@router.get("/api/v1/table-qr-codes/sheet", dependencies=[Depends(require_business(RESTAURANT))])
async def table_sheet(accent: str = "safran", current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    """Lot 108 b — les chevalets de toutes les tables (A6, 4 par page A4), prêts à imprimer."""
    from fastapi.responses import HTMLResponse

    from app.core.config import get_settings
    from app.models.tenant import Tenant
    from app.services import resto_posters, vitrine_posters
    from app.services.launch_kit import qr_svg

    base = get_settings().public_base_url.rstrip("/")
    tables = [(qr.label, qr_svg(f"{base}/t/{qr.code}")) for qr in await _tables(db, current_user.tenant_id)]
    try:
        page = resto_posters.table_sheet(await db.get(Tenant, current_user.tenant_id), tables, accent)
    except vitrine_posters.PosterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/api/v1/table-qr-codes/{qr_id}/qr.svg", dependencies=[Depends(require_business(RESTAURANT))])
async def table_qr_image(qr_id: UUID, current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Le QR code d'une table, en SVG (net à toutes les tailles), en attendant le chevalet à imprimer."""
    from fastapi.responses import Response

    from app.core.config import get_settings
    from app.services.launch_kit import qr_svg

    qr = (await db.execute(select(TableQrCode).where(TableQrCode.tenant_id == current_user.tenant_id,
                                                     TableQrCode.id == qr_id))).scalar_one_or_none()
    if qr is None:
        raise HTTPException(status_code=404, detail="Table introuvable")
    svg = qr_svg(f"{get_settings().public_base_url.rstrip('/')}/t/{qr.code}")
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.get("/t/{code}")
async def open_table(code: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Public (cible du QR posé sur la table) : compte le scan puis ouvre WhatsApp avec le numéro de la table."""
    from app.models.tenant import Tenant
    from app.services import link_preview

    qr = (await db.execute(select(TableQrCode).where(TableQrCode.code == code))).scalar_one_or_none()
    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == qr.tenant_id))).scalar_one_or_none() \
        if qr is not None else None
    if qr is None or account is None or not account.display_phone_number:
        raise HTTPException(status_code=404, detail="Ce QR code n'est pas encore relié à un numéro WhatsApp")
    if not link_preview.is_crawler(request.headers.get("user-agent")):  # l'aperçu d'un lien partagé n'est pas un scan
        qr.scan_count += 1
        await db.commit()
    tenant = await db.get(Tenant, qr.tenant_id)
    shop = tenant.name if tenant else ""
    digits = "".join(ch for ch in account.display_phone_number if ch.isdigit())
    wa_link = f"https://wa.me/{digits}?text={quote(TABLE_TEXT.format(label=qr.label) + f' [T:{qr.code}]')}"
    return link_preview.page(f"Table {qr.label} — {shop}", "Commandez depuis votre table sur WhatsApp", None, wa_link, shop)
