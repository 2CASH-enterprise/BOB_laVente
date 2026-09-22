from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.integrations.ecommerce.dependency import get_shopify_client_factory
from app.models.ecommerce_connection import EcommerceConnection, EcommercePlatform, SyncStatus
from app.schemas.catalog import CsvImportResponse
from app.schemas.ecommerce import EcommerceConnectionResponse, ShopifyConnectRequest
from app.services.audit import log_audit_event
from app.services.shopify_sync import sync_shopify_catalog

router = APIRouter(prefix="/api/v1/integrations/shopify", tags=["integrations"])


async def _get_connection(db: AsyncSession, tenant_id) -> EcommerceConnection | None:
    stmt = select(EcommerceConnection).where(
        EcommerceConnection.tenant_id == tenant_id, EcommerceConnection.platform == EcommercePlatform.SHOPIFY
    )
    return (await db.execute(stmt)).scalar_one_or_none()


@router.post("/connect", response_model=EcommerceConnectionResponse, dependencies=[Depends(require_role("ADMIN"))])
async def connect_shopify(
    payload: ShopifyConnectRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    client_factory=Depends(get_shopify_client_factory),
):
    """
    Teste réellement les identifiants (récupération de la devise de la boutique) avant
    d'enregistrer quoi que ce soit — jamais de connexion stockée sans validation.
    """
    from app.models.tenant import Tenant

    tenant = await db.get(Tenant, current_user.tenant_id)
    if tenant is None or not tenant.is_paid:
        raise HTTPException(status_code=403, detail="Cette intégration nécessite un plan payant")

    client = client_factory(payload.shop_domain, payload.access_token)
    try:
        currency = await client.fetch_shop_currency()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Connexion à Shopify impossible : {exc}") from exc

    existing = await _get_connection(db, current_user.tenant_id)
    if existing is not None:
        existing.shop_domain = payload.shop_domain
        existing.access_token = payload.access_token
        existing.currency = currency
        connection = existing
    else:
        connection = EcommerceConnection(
            tenant_id=current_user.tenant_id,
            platform=EcommercePlatform.SHOPIFY,
            shop_domain=payload.shop_domain,
            access_token=payload.access_token,
            currency=currency,
        )
        db.add(connection)

    await log_audit_event(
        db,
        actor=str(current_user.user_id),
        action="SHOPIFY_CONNECTED",
        tenant_id=current_user.tenant_id,
        details={"shop_domain": payload.shop_domain},
    )
    await db.commit()
    await db.refresh(connection)
    return connection


@router.get("/status", response_model=EcommerceConnectionResponse)
async def shopify_status(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    connection = await _get_connection(db, current_user.tenant_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Aucune boutique Shopify connectée")
    return connection


@router.post("/sync", response_model=CsvImportResponse, dependencies=[Depends(require_role("MANAGER"))])
async def sync_shopify(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    client_factory=Depends(get_shopify_client_factory),
):
    connection = await _get_connection(db, current_user.tenant_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Aucune boutique Shopify connectée")

    client = client_factory(connection.shop_domain, connection.access_token)
    result = await sync_shopify_catalog(db, current_user.tenant_id, connection, client=client)

    await log_audit_event(
        db,
        actor=str(current_user.user_id),
        action="SHOPIFY_SYNC",
        tenant_id=current_user.tenant_id,
        details={"imported": result.imported, "updated": result.updated, "failed": result.failed},
    )
    await db.commit()
    return result
