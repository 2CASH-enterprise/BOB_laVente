"""
Lot 59 — la liste de clients du commerçant, depuis et vers Excel (les trois secteurs).

Déclaré AVANT le routeur des clients : sinon « /export.xlsx » serait pris pour l'identifiant d'un client.
"""
import json

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.tenant import Tenant
from app.services import customer_import
from app.services.audit import log_audit_event
from app.services.spreadsheet import MAX_BYTES, XLSX_MEDIA, to_xlsx

router = APIRouter(prefix="/api/v1/customers", tags=["customer-import"])


async def _read(file: UploadFile) -> bytes:
    raw = await file.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Fichier trop volumineux (5 Mo au plus)")
    return raw


@router.post("/import/preview", dependencies=[Depends(require_role("MANAGER"))])
async def import_preview(
    file: UploadFile,
    sheet: str | None = Form(None),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Correspondance proposée des colonnes et premières lignes ; rien n'est enregistré."""
    try:
        return customer_import.preview(await _read(file), file.filename or "", sheet or None)
    except customer_import.ImportError_ as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/import", dependencies=[Depends(require_role("MANAGER"))])
async def import_customers(
    file: UploadFile,
    mapping: str = Form(...),
    sheet: str | None = Form(None),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        chosen = json.loads(mapping)
    except ValueError:
        raise HTTPException(status_code=422, detail="Correspondance des colonnes illisible") from None
    tenant = await db.get(Tenant, current_user.tenant_id)
    try:
        report = await customer_import.import_customers(db, tenant, await _read(file), file.filename or "", sheet or None, chosen)
    except customer_import.ImportError_ as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit_event(db, actor=str(current_user.user_id), action="CUSTOMERS_IMPORTED", tenant_id=tenant.id,
                          details={k: report[k] for k in ("created", "completed", "unchanged", "total")})
    await db.commit()
    return report


@router.get("/export.xlsx", dependencies=[Depends(require_role("AGENT"))])
async def export_customers(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Response:
    tenant = await db.get(Tenant, current_user.tenant_id)
    header, rows = await customer_import.export_rows(db, tenant)
    await log_audit_event(db, actor=str(current_user.user_id), action="CUSTOMERS_EXPORTED", tenant_id=tenant.id,
                          details={"count": len(rows)})
    await db.commit()
    return Response(to_xlsx("Clients", header, rows), media_type=XLSX_MEDIA,
                    headers={"Content-Disposition": 'attachment; filename="clients.xlsx"'})
