from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.core.pending_store import PendingStore, get_pending_store
from app.integrations.ecommerce.dependency import get_meta_catalog_client_factory
from app.integrations.ecommerce.meta_catalog_client import describe_catalog_error, get_meta_catalog_oauth
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
    from app.models.tenant import Tenant

    tenant = await db.get(Tenant, current_user.tenant_id)
    if tenant is None or not tenant.is_paid:
        raise HTTPException(status_code=403, detail="Cette intégration nécessite un plan payant")

    client = client_factory(payload.catalog_id, payload.access_token)
    try:
        await client.fetch_catalog_info()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"Connexion au Meta Commerce Catalog impossible : {describe_catalog_error(exc)}"
        ) from exc

    connection = await _save_connection(db, current_user, payload.catalog_id.strip(), payload.access_token, via="manual")
    await db.commit()
    await db.refresh(connection)
    return connection


async def _save_connection(db: AsyncSession, current_user: CurrentUser, catalog_id: str, access_token: str, via: str):
    existing = await _get_connection(db, current_user.tenant_id)
    if existing is not None:
        if existing.shop_domain != catalog_id:
            existing.last_synced_at = None
            existing.last_sync_status = SyncStatus.NEVER_SYNCED
        existing.shop_domain = catalog_id
        existing.access_token = access_token
        connection = existing
    else:
        connection = EcommerceConnection(
            tenant_id=current_user.tenant_id,
            platform=EcommercePlatform.META_CATALOG,
            shop_domain=catalog_id,
            access_token=access_token,
        )
        db.add(connection)
    # Lot 21 : synchronisation automatique chaque nuit pour tout catalogue connecté.
    connection.auto_sync_enabled = True
    await log_audit_event(
        db,
        actor=str(current_user.user_id),
        action="META_CATALOG_CONNECTED",
        tenant_id=current_user.tenant_id,
        details={"catalog_id": catalog_id, "via": via},
    )
    await db.flush()
    return connection


# --- Lot 21 : connexion en un clic (Facebook Login for Business) ---------------------------

PENDING_TTL_SECONDS = 600  # le jeton attend le choix du catalogue 10 minutes au plus
MAX_CATALOGS_LISTED = 20


def _pending_key(tenant_id) -> str:
    return f"meta-catalog-oauth:{tenant_id}"


class OAuthCallback(BaseModel):
    code: str = Field(min_length=1, max_length=2048)


class CatalogChoice(BaseModel):
    catalog_id: str = Field(min_length=1, max_length=64)


@router.get("/login-config")
async def meta_catalog_login_config(current_user: CurrentUser = Depends(get_current_user)):
    """Valeurs publiques du bouton (identifiants d'app et de configuration, jamais de secret)."""
    from app.core.config import get_settings

    settings = get_settings()
    return {
        "available": bool(settings.meta_catalog_login_config_id and settings.whatsapp_app_id),
        "app_id": settings.whatsapp_app_id,
        "config_id": settings.meta_catalog_login_config_id,
        "graph_api_version": settings.whatsapp_graph_api_version,
    }


async def _require_paid(db: AsyncSession, tenant_id) -> None:
    from app.models.tenant import Tenant

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not tenant.is_paid:
        raise HTTPException(status_code=403, detail="Cette intégration nécessite un plan payant")


async def _connect_and_sync(db, current_user, catalog_id, access_token, client_factory) -> dict:
    connection = await _save_connection(db, current_user, catalog_id, access_token, via="oauth")
    await db.commit()
    result = await sync_meta_catalog(db, current_user.tenant_id, connection,
                                     client=client_factory(catalog_id, access_token))
    await db.commit()
    return {"status": "connected", "catalog_id": catalog_id, "sync": result.model_dump()}


@router.post("/oauth/callback", dependencies=[Depends(require_role("ADMIN"))])
async def meta_catalog_oauth_callback(
    payload: OAuthCallback,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    oauth=Depends(get_meta_catalog_oauth),
    client_factory=Depends(get_meta_catalog_client_factory),
    pending: PendingStore = Depends(get_pending_store),
):
    """
    Échange le code contre un jeton (côté serveur), lit les catalogues que le commerçant a
    partagés, puis connecte directement (un seul) ou propose le choix (plusieurs). Le jeton
    n'est JAMAIS renvoyé au navigateur.
    """
    await _require_paid(db, current_user.tenant_id)
    try:
        token = await oauth.exchange_code(payload.code)
        granted, counts = await oauth.granted_catalogs(token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Connexion Meta impossible : {describe_catalog_error(exc)}") from exc

    import logging

    logging.getLogger(__name__).info("Catalogues Meta partagés pour la boutique %s : %s", current_user.tenant_id, counts)
    if not granted:
        raise HTTPException(
            status_code=400,
            detail="Aucun catalogue n'a été partagé. Recommencez et cochez le catalogue de votre boutique dans la fenêtre Meta. "
                   f"(Diagnostic Meta : {counts['autorisations']} dans les autorisations, {counts['attribues']} attribué(s), "
                   f"{counts['entreprise']} dans l'entreprise.)",
        )

    catalogs = []
    for item in granted[:MAX_CATALOGS_LISTED]:
        name = item.get("name")
        if not name:
            try:
                name = (await client_factory(item["id"], token).fetch_catalog_info()).get("name")
            except Exception:  # noqa: BLE001 — un catalogue illisible n'est simplement pas proposé
                continue
        catalogs.append({"id": item["id"], "name": str(name or f"Catalogue {item['id']}")})
    if not catalogs:
        raise HTTPException(status_code=400, detail="Les catalogues partagés ne sont pas lisibles avec cette autorisation.")

    if len(catalogs) == 1:
        return await _connect_and_sync(db, current_user, catalogs[0]["id"], token, client_factory)

    await pending.put(_pending_key(current_user.tenant_id), {"token": token, "catalogs": catalogs}, PENDING_TTL_SECONDS)
    return {"status": "choose", "catalogs": catalogs}


@router.post("/oauth/select", dependencies=[Depends(require_role("ADMIN"))])
async def meta_catalog_oauth_select(
    payload: CatalogChoice,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    client_factory=Depends(get_meta_catalog_client_factory),
    pending: PendingStore = Depends(get_pending_store),
):
    await _require_paid(db, current_user.tenant_id)
    key = _pending_key(current_user.tenant_id)
    stored = await pending.get(key)
    if stored is None:
        raise HTTPException(status_code=410, detail="Le choix a expiré : cliquez à nouveau sur « Connecter mon catalogue Facebook ».")
    if payload.catalog_id not in {c["id"] for c in stored["catalogs"]}:
        raise HTTPException(status_code=400, detail="Ce catalogue ne fait pas partie de ceux que vous avez partagés.")
    await pending.delete(key)  # usage unique
    return await _connect_and_sync(db, current_user, payload.catalog_id, stored["token"], client_factory)


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
