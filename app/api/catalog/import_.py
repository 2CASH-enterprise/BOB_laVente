from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.schemas.catalog import CsvImportResponse
from app.services.catalog_import import import_catalog_csv

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])

MAX_CSV_SIZE_BYTES = 10 * 1024 * 1024  # 10 Mo


def _read_sheet(raw: bytes, name: str) -> tuple[list[str], list[list[str]]]:
    from app.services import spreadsheet

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
    return header, rows


@router.post("/import-csv", response_model=CsvImportResponse, dependencies=[Depends(require_role("MANAGER"))])
async def import_csv(
    file: UploadFile,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Section 24 — import du catalogue ; lot 78 B : fichier Excel (.xlsx) ou CSV (« , » ou « ; »), en-têtes français."""
    from app.services.catalog_import import table_to_import_csv

    name = (file.filename or "").lower()
    if not name.endswith((".csv", ".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Formats acceptés : Excel (.xlsx) ou CSV")

    raw = await file.read()
    header, rows = _read_sheet(raw, name)

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


# --- Lot 88 : « Vérifiez avant d'importer » (Excel, CSV, PDF) --------------------------------------------------------

class PreviewTable(BaseModel):
    header: list[str]
    rows: list[list[str | None]]
    import_id: UUID | None = None


async def _tenant_and_slots(db, current_user):
    from app.models.tenant import Tenant
    from app.services.plan_limits import remaining_product_slots

    tenant = await db.get(Tenant, current_user.tenant_id)
    return tenant, await remaining_product_slots(db, current_user.tenant_id, tenant.is_paid)


async def _checked(db, tenant, max_new, header, rows, **extra) -> dict:
    from app.services import catalog_preview

    issues = await catalog_preview.check(db, tenant, header, rows, max_new)
    return catalog_preview.preview(header, rows, issues, **extra)


def _start_ai_reading(import_id) -> None:
    """Lecture par l'IA en arrière-plan (worker)."""
    from app.workers.catalog_pdf import read_catalog_pdf_task

    read_catalog_pdf_task.delay(str(import_id))


def _generated_notes(rows: list, generated: list[int]) -> list[dict]:
    return [{"row": i, "field": "SKU", "level": "info", "message": "Référence proposée par Bob (le catalogue n'en donne pas)"}
            for i in generated if i < len(rows)]


@router.post("/import-preview", dependencies=[Depends(require_role("MANAGER"))])
async def import_preview(file: UploadFile, current_user: CurrentUser = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)) -> dict:
    """Le fichier est lu et montré en tableau à vérifier ; rien n'est importé à cette étape."""
    from app.core.config import get_settings
    from app.models.catalog_import import CatalogImport
    from app.services import catalog_pdf, catalog_preview

    name = (file.filename or "").lower()
    if not name.endswith((".csv", ".xlsx", ".xlsm", ".pdf")):
        raise HTTPException(status_code=400, detail="Formats acceptés : Excel (.xlsx), CSV ou PDF")
    raw = await file.read()
    tenant, max_new = await _tenant_and_slots(db, current_user)
    if not name.endswith(".pdf"):
        header, rows = catalog_preview.to_canonical(tenant, *_read_sheet(raw, name))
        return await _checked(db, tenant, max_new, header, rows, source="SHEET")
    try:
        read = catalog_pdf.read_tables(raw)
    except catalog_pdf.PdfError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if read["header"] and read["rows"]:  # vrai tableau : lu sans IA, gratuit
        header, rows = catalog_pdf.with_images(read["header"], read["rows"], read["images"])
        header, rows = catalog_preview.to_canonical(tenant, header, rows)
        db.add(CatalogImport(tenant_id=tenant.id, source="PDF_TABLE", status="READY", filename=(file.filename or "")[:200],
                             pages=read["pages"]))
        await db.commit()
        return await _checked(db, tenant, max_new, header, rows, source="PDF_TABLE", pages=read["pages"])
    settings = get_settings()
    quota = await catalog_pdf.ai_quota(db, tenant)
    refusal = catalog_pdf.ai_refusal(tenant, quota, bool(settings.mistral_api_key))
    if refusal:
        raise HTTPException(status_code=422, detail=refusal)
    record = CatalogImport(tenant_id=tenant.id, source="PDF_AI", status="PROCESSING", filename=(file.filename or "")[:200],
                           pages=read["pages"])
    db.add(record)
    await db.flush()
    catalog_pdf.STORE.mkdir(parents=True, exist_ok=True)
    (catalog_pdf.STORE / f"{record.id}.pdf").write_bytes(raw)
    await db.commit()
    try:
        _start_ai_reading(record.id)
    except Exception:  # noqa: BLE001 — file d'attente indisponible : l'import échoue proprement, rien n'est décompté
        record.status, record.error = "FAILED", "Lecture en arrière-plan indisponible"
        (catalog_pdf.STORE / f"{record.id}.pdf").unlink(missing_ok=True)
        await db.commit()
        raise HTTPException(status_code=503, detail="La lecture du PDF est momentanément indisponible, réessayez.") from None
    return {"id": str(record.id), "status": "PROCESSING", "pages": read["pages"], "source": "PDF_AI"}


@router.get("/import-preview/{import_id}", dependencies=[Depends(require_role("MANAGER"))])
async def import_preview_status(import_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)) -> dict:
    from app.models.catalog_import import CatalogImport
    from app.services import catalog_pdf, catalog_preview

    record = await db.get(CatalogImport, import_id)
    if record is None or record.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=404, detail="Import introuvable")
    if catalog_pdf.timed_out(record):
        record.status, record.error = "FAILED", "La lecture a pris trop de temps"
        (catalog_pdf.STORE / f"{record.id}.pdf").unlink(missing_ok=True)
        await db.commit()
    if record.status == "PROCESSING":
        return {"id": str(record.id), "status": "PROCESSING", "pages": record.pages}
    if record.status == "FAILED":
        return {"id": str(record.id), "status": "FAILED",
                "error": (record.error or "Lecture impossible") + " : rien n'a été décompté, vous pouvez réessayer."}
    if record.status == "IMPORTED" or not record.result:
        return {"id": str(record.id), "status": record.status}
    tenant, max_new = await _tenant_and_slots(db, current_user)
    header, rows = catalog_preview.to_canonical(tenant, record.result["header"], record.result["rows"])
    data = await _checked(db, tenant, max_new, header, rows, source="PDF_AI", pages=record.pages, id=str(record.id),
                          status="READY")
    data["issues"] += _generated_notes(rows, record.result.get("generated") or [])
    return data


@router.post("/import-check", dependencies=[Depends(require_role("MANAGER"))])
async def import_check(payload: PreviewTable, current_user: CurrentUser = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)) -> dict:
    """Revérifie le tableau corrigé par le commerçant (rien n'est enregistré)."""
    from app.services import catalog_preview

    try:
        header, rows = catalog_preview.clean_table(payload.header, payload.rows)
    except catalog_preview.PreviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    tenant, max_new = await _tenant_and_slots(db, current_user)
    return await _checked(db, tenant, max_new, header, rows)


@router.post("/import-rows", response_model=CsvImportResponse, dependencies=[Depends(require_role("MANAGER"))])
async def import_rows(payload: PreviewTable, current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    """Import du tableau vérifié : mêmes règles que l'import d'un fichier ; les lignes encore en erreur sont ignorées."""
    from app.models.catalog_import import CatalogImport
    from app.services import catalog_preview

    try:
        header, rows = catalog_preview.clean_table(payload.header, payload.rows)
    except catalog_preview.PreviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    tenant, max_new = await _tenant_and_slots(db, current_user)
    options = catalog_preview.import_options(tenant)
    if payload.import_id:
        record = await db.get(CatalogImport, payload.import_id)
        if record is not None and record.tenant_id == tenant.id:
            record.status, record.result = "IMPORTED", None  # l'aperçu n'est plus gardé
    return await import_catalog_csv(db, tenant.id, catalog_preview.to_csv(header, rows), max_new_products=max_new, **options)


@router.get("/pdf-quota", dependencies=[Depends(require_role("MANAGER"))])
async def pdf_quota(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    from app.core.config import get_settings
    from app.models.tenant import Tenant
    from app.services import catalog_pdf

    tenant = await db.get(Tenant, current_user.tenant_id)
    return {**await catalog_pdf.ai_quota(db, tenant), "configured": bool(get_settings().mistral_api_key)}
