"""
Synchronisation du catalogue depuis Shopify (section 25, niveau 2 — API).
Même principe de robustesse que l'import CSV (section 24) : une ligne en échec
n'interrompt jamais les autres, jamais de prix/stock inventé en cas de champ absent.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.ecommerce.shopify_client import ShopifyClient, map_shopify_product
from app.models.ecommerce_connection import EcommerceConnection, SyncStatus
from app.models.product import Product
from app.repositories.category_repository import CategoryRepository
from app.repositories.product_repository import ProductRepository
from app.schemas.catalog import CsvImportResponse

PLATFORM_SHOPIFY = "SHOPIFY"


async def sync_shopify_catalog(
    db: AsyncSession,
    tenant_id,
    connection: EcommerceConnection,
    client: ShopifyClient | None = None,
) -> CsvImportResponse:
    """
    Réutilise CsvImportResponse comme format de résultat : même structure de compte-rendu
    (importés/mis à jour/échecs/disponibles) que l'import CSV, pour rester cohérent côté
    dashboard quelle que soit la source d'import.
    """
    shopify_client = client or ShopifyClient(connection.shop_domain, connection.access_token)

    product_repo = ProductRepository(db)
    category_repo = CategoryRepository(db)

    try:
        shopify_products = await shopify_client.fetch_products()
    except Exception as exc:  # noqa: BLE001 — jamais planter le tenant, journaliser et remonter proprement
        connection.last_sync_status = SyncStatus.FAILED
        await db.commit()
        return CsvImportResponse(
            total_rows=0, imported=0, updated=0, failed=0, available=0, unavailable=0,
            errors=[f"Échec de connexion à Shopify : {exc}"],
        )

    total = imported = updated = failed = available = unavailable = 0
    errors: list[str] = []

    for raw in shopify_products:
        total += 1
        try:
            mapped = map_shopify_product(raw)
            price = Decimal(str(mapped["price"]))
            if price <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError, TypeError, KeyError):
            failed += 1
            errors.append(f"Produit Shopify {raw.get('id')} : prix invalide ou absent")
            continue

        category_id = None
        if mapped["category"]:
            category = await category_repo.get_or_create_by_name(tenant_id, mapped["category"])
            category_id = category.id

        currency = connection.currency or "USD"
        existing = await product_repo.get_by_external_id(tenant_id, PLATFORM_SHOPIFY, mapped["external_id"])

        if existing is not None:
            existing.name = mapped["name"]
            existing.description = mapped["description"]
            existing.price = price
            existing.currency = currency
            existing.stock_quantity = mapped["stock_quantity"]
            existing.category_id = category_id
            existing.image_url = mapped["image_url"]
            existing.active = mapped["active"]
            updated += 1
        else:
            db.add(
                Product(
                    tenant_id=tenant_id,
                    external_source=PLATFORM_SHOPIFY,
                    external_id=mapped["external_id"],
                    sku=mapped["sku"],
                    name=mapped["name"],
                    description=mapped["description"],
                    price=price,
                    currency=currency,
                    stock_quantity=mapped["stock_quantity"],
                    category_id=category_id,
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
