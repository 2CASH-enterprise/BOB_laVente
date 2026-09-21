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
    """Section 24 — import du catalogue au format CSV."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Seul le format CSV est accepté (le format Excel suit)")

    raw = await file.read()
    if len(raw) > MAX_CSV_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Fichier trop volumineux (max 10 Mo)")

    try:
        content = raw.decode("utf-8-sig")  # tolère le BOM Excel
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Encodage du fichier non supporté, utilisez UTF-8") from None

    return await import_catalog_csv(db, current_user.tenant_id, content)
