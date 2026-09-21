"""
Synchronisation du catalogue depuis Meta Commerce Catalog (section 25, niveau 2).
Structure jumelle de shopify_sync.py — seule la source et le mapping diffèrent,
la logique d'upsert/garde-fous est identique.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.ecommerce.meta_catalog_client import MetaCatalogClient, map_meta_catalog_product
from app.models.ecommerce_connection import EcommerceConnection, SyncStatus
from app.models.product import Product
from app.repositories.product_repository import ProductRepository
from app.schemas.catalog import CsvImportResponse

PLATFORM_META_CATALOG = "META_CATALOG"


async def sync_meta_catalog(
    db: AsyncSession,
    tenant_id,
    connection: EcommerceConnection,
    client: MetaCatalogClient | None = None,
) -> CsvImportResponse:
    meta_client = client or MetaCatalogClient(connection.shop_domain, connection.access_token)
    product_repo = ProductRepository(db)

    try:
        raw_products = await meta_client.fetch_products()
    except Exception as exc:  # noqa: BLE001
        connection.last_sync_status = SyncStatus.FAILED
        await db.commit()
        return CsvImportResponse(
            total_rows=0, imported=0, updated=0, failed=0, available=0, unavailable=0,
            errors=[f"Échec de connexion au Meta Commerce Catalog : {exc}"],
        )

    total = imported = updated = failed = available = unavailable = 0
    errors: list[str] = []

    for raw in raw_products:
        total += 1
        try:
            mapped = map_meta_catalog_product(raw)
            price = Decimal(str(mapped["price"]))
            if price <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError, TypeError, KeyError):
            failed += 1
            errors.append(f"Produit Meta {raw.get('id')} : prix invalide ou absent")
            continue

        currency = (mapped["currency"] or "").upper()
        if len(currency) != 3:
            failed += 1
            errors.append(f"Produit Meta {raw.get('id')} : devise invalide")
            continue

        existing = await product_repo.get_by_external_id(tenant_id, PLATFORM_META_CATALOG, mapped["external_id"])

        if existing is not None:
            existing.name = mapped["name"]
            existing.description = mapped["description"]
            existing.price = price
            existing.currency = currency
            existing.stock_quantity = mapped["stock_quantity"]
            existing.image_url = mapped["image_url"]
            existing.active = mapped["active"]
            updated += 1
        else:
            db.add(
                Product(
                    tenant_id=tenant_id,
                    external_source=PLATFORM_META_CATALOG,
                    external_id=mapped["external_id"],
                    sku=mapped["sku"],
                    name=mapped["name"],
                    description=mapped["description"],
                    price=price,
                    currency=currency,
                    stock_quantity=mapped["stock_quantity"],
                    image_url=mapped["image_url"],
                    active=mapped["active"],
                )
            )
            imported += 1

        if mapped["active"] and mapped["stock_quantity"] > 0:
            available += 1
        else:
            unavailable += 1

    connection.last_synced_at = datetime.now(timezone.utc)
    connection.last_sync_status = SyncStatus.SUCCESS
    await db.commit()

    return CsvImportResponse(
        total_rows=total, imported=imported, updated=updated, failed=failed,
        available=available, unavailable=unavailable, errors=errors,
    )
