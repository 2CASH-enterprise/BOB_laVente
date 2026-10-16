"""
Lot 21 — synchronisation automatique des catalogues Meta, chaque nuit.

Chaque boutique est traitée dans sa propre session : l'échec de l'une (jeton révoqué, Meta
indisponible) est enregistré sur SA connexion et n'empêche jamais les autres. Aucun jeton ni
adresse de requête dans les journaux.
"""
import asyncio
import logging

from sqlalchemy import select

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def sync_all_meta_catalogs(session_factory=None) -> dict:
    engine = None
    if session_factory is None:
        # Moteur dédié à CETTE exécution (asyncio.run crée une nouvelle boucle à chaque tâche :
        # des connexions gardées d'une exécution précédente y seraient inutilisables).
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.core.config import get_settings

        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    try:
        return await _sync_all(session_factory)
    finally:
        if engine is not None:
            await engine.dispose()


async def _sync_all(factory) -> dict:
    from app.integrations.ecommerce.meta_catalog_client import MetaCatalogClient, describe_catalog_error
    from app.models.ecommerce_connection import EcommerceConnection, EcommercePlatform
    from app.services.meta_catalog_sync import sync_meta_catalog

    async with factory() as db:
        ids = (await db.execute(
            # Tout catalogue connecté, y compris ceux branchés avant le lot 21.
            select(EcommerceConnection.id).where(EcommerceConnection.platform == EcommercePlatform.META_CATALOG)
        )).scalars().all()

    report = {"synced": 0, "failed": 0}
    for connection_id in ids:
        async with factory() as db:
            connection = await db.get(EcommerceConnection, connection_id)
            if connection is None:
                continue
            try:
                client = MetaCatalogClient(connection.shop_domain, connection.access_token)
                result = await sync_meta_catalog(db, connection.tenant_id, connection, client=client)
                failed_connection = result.total_rows == 0 and bool(result.errors)
                report["failed" if failed_connection else "synced"] += 1
            except Exception as exc:  # noqa: BLE001
                report["failed"] += 1
                logger.warning("Synchronisation Meta échouée pour la boutique %s : %s",
                               connection.tenant_id, describe_catalog_error(exc))
    return report


@celery_app.task(name="app.workers.catalog_sync.sync_meta_catalogs_task")
def sync_meta_catalogs_task() -> dict:
    return asyncio.run(sync_all_meta_catalogs())


# --- Lot 78 : boutiques Shopify, chaque nuit (prix, stock, variantes, fiches) ------------------------------------


async def sync_all_shopify(session_factory=None) -> dict:
    engine = None
    if session_factory is None:
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.core.config import get_settings

        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    try:
        return await _sync_all_shopify(session_factory)
    finally:
        if engine is not None:
            await engine.dispose()


async def _sync_all_shopify(factory, client_factory=None) -> dict:
    from app.integrations.ecommerce.shopify_client import ShopifyClient
    from app.models.ecommerce_connection import EcommerceConnection, EcommercePlatform
    from app.services.shopify_sync import sync_shopify_catalog

    async with factory() as db:
        ids = (await db.execute(
            select(EcommerceConnection.id).where(EcommerceConnection.platform == EcommercePlatform.SHOPIFY)
        )).scalars().all()

    report = {"synced": 0, "failed": 0}
    for connection_id in ids:
        async with factory() as db:
            connection = await db.get(EcommerceConnection, connection_id)
            if connection is None:
                continue
            try:
                client = (client_factory or ShopifyClient)(connection.shop_domain, connection.access_token)
                result = await sync_shopify_catalog(db, connection.tenant_id, connection, client=client)
                report["failed" if result.total_rows == 0 and result.errors else "synced"] += 1
            except Exception:  # noqa: BLE001 — une boutique en échec n'empêche jamais les autres
                report["failed"] += 1
                logger.warning("Synchronisation Shopify échouée pour la boutique %s", connection.tenant_id)
    return report


@celery_app.task(name="app.workers.catalog_sync.sync_shopify_task")
def sync_shopify_task() -> dict:
    return asyncio.run(sync_all_shopify())
