from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.schemas.catalog import CsvImportResponse
from app.services.catalog_import import import_catalog_csv

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])

MAX_CSV_SIZE_BYTES = 10 * 1024 * 1024  # 10 Mo


@router.post("/import-csv", response_model=CsvImportResponse, dependencies=[Depends(require_role("MANAGER"))])
async def import_csv(
    file: UploadFile,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Section 24 — import du catalogue ; lot 78 B : fichier Excel (.xlsx) ou CSV (« , » ou « ; »), en-têtes français."""
    from app.services import spreadsheet
    from app.services.catalog_import import table_to_import_csv

    name = (file.filename or "").lower()
    if not name.endswith((".csv", ".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Formats acceptés : Excel (.xlsx) ou CSV")

    raw = await file.read()
    if len(raw) > MAX_CSV_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Fichier trop volumineux (max 10 Mo)")

    try:
        if name.endswith(".csv"):
            table = [row for row in spreadsheet._rows_from_csv(raw) if any(row)]
            header, rows = (table[0], table[1:]) if table else ([], [])
        else:
            read = spreadsheet.read_table(raw, name)
            header, rows = read["header"], read["rows"]
    except spreadsheet.SpreadsheetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if not header:
        raise HTTPException(status_code=400, detail="Le fichier est vide")

    from app.models.tenant import Tenant
    from app.services.plan_limits import remaining_product_slots

    tenant = await db.get(Tenant, current_user.tenant_id)
    max_new = await remaining_product_slots(db, current_user.tenant_id, tenant.is_paid)

    from app.services.business_type import is_dealership, is_insurance, is_real_estate, is_store

    store, dealership, estate = is_store(tenant), is_dealership(tenant), is_real_estate(tenant)
    content = table_to_import_csv(header, rows, keep_vehicle=dealership, keep_property=estate,
                                  default_currency=None if is_insurance(tenant) else tenant.currency)
    return await import_catalog_csv(db, current_user.tenant_id, content, max_new_products=max_new,
                                    with_vehicles=dealership, without_prices=is_insurance(tenant),
                                    with_properties=estate, with_sheets=store,
                                    default_category=tenant.default_bob_category)
