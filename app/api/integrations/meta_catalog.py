from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.integrations.ecommerce.dependency import get_meta_catalog_client_factory
from app.models.ecommerce_connection import EcommerceConnection, EcommercePlatform, SyncStatus
from app.schemas.catalog import CsvImportResponse
from app.schemas.ecommerce import EcommerceConnectionResponse, MetaCatalogConnectRequest
from app.services.audit import log_audit_event
from app.services.meta_catalog_sync import sync_meta_catalog

router = APIRouter(prefix="/api/v1/integrations/meta-catalog", tags=["integrations"])


async def _get_connection(db: AsyncSession, tenant_id) -> EcommerceConnection | None:
    stmt = select(EcommerceConnection).where(
        EcommerceConnection.tenant_id == tenant_id, EcommerceConnection.platform == EcommercePlatform.META_CATALOG
    )
    return (await db.execute(stmt)).scalar_one_or_none()


@router.post("/connect", response_model=EcommerceConnectionResponse, dependencies=[Depends(require_role("ADMIN"))])
async def connect_meta_catalog(
    payload: MetaCatalogConnectRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    client_factory=Depends(get_meta_catalog_client_factory),
):
    """Valide réellement l'accès au catalogue (Graph API) avant d'enregistrer quoi que ce soit."""
    client = client_factory(payload.catalog_id, payload.access_token)
    try:
        await client.fetch_catalog_info()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Connexion au Meta Commerce Catalog impossible : {exc}") from exc

    existing = await _get_connection(db, current_user.tenant_id)
    if existing is not None:
        existing.shop_domain = payload.catalog_id
        existing.access_token = payload.access_token
        connection = existing
    else:
        connection = EcommerceConnection(
            tenant_id=current_user.tenant_id,
            platform=EcommercePlatform.META_CATALOG,
            shop_domain=payload.catalog_id,
            access_token=payload.access_token,
        )
        db.add(connection)

    await log_audit_event(
        db,
        actor=str(current_user.user_id),
        action="META_CATALOG_CONNECTED",
        tenant_id=current_user.tenant_id,
        details={"catalog_id": payload.catalog_id},
    )
    await db.commit()
    await db.refresh(connection)
    return connection


@router.get("/status", response_model=EcommerceConnectionResponse)
async def meta_catalog_status(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    connection = await _get_connection(db, current_user.tenant_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Aucun Meta Commerce Catalog connecté")
    return connection


@router.post("/sync", response_model=CsvImportResponse, dependencies=[Depends(require_role("MANAGER"))])
async def sync_meta_catalog_endpoint(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    client_factory=Depends(get_meta_catalog_client_factory),
):
    connection = await _get_connection(db, current_user.tenant_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Aucun Meta Commerce Catalog connecté")

    client = client_factory(connection.shop_domain, connection.access_token)
    result = await sync_meta_catalog(db, current_user.tenant_id, connection, client=client)

    await log_audit_event(
        db,
        actor=str(current_user.user_id),
        action="META_CATALOG_SYNC",
        tenant_id=current_user.tenant_id,
        details={"imported": result.imported, "updated": result.updated, "failed": result.failed},
    )
    await db.commit()
    return result
