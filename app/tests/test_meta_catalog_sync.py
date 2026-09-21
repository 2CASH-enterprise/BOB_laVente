import pytest

from app.models.ecommerce_connection import EcommerceConnection, EcommercePlatform, SyncStatus
from app.models.tenant import Tenant
from app.services.meta_catalog_sync import sync_meta_catalog
from app.tests.fakes_meta_catalog import FakeMetaCatalogClient, make_meta_product


async def _setup_connection(db_session, email: str) -> tuple[Tenant, EcommerceConnection]:
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    connection = EcommerceConnection(
        tenant_id=tenant.id, platform=EcommercePlatform.META_CATALOG,
        shop_domain="catalog_123", access_token="fake-token",
    )
    db_session.add(connection)
    await db_session.commit()
    await db_session.refresh(connection)
    return tenant, connection


@pytest.mark.asyncio
async def test_sync_creates_products_with_currency_from_meta(db_session, unique_email):
    tenant, connection = await _setup_connection(db_session, unique_email)
    products = [make_meta_product("1", "Samsung A56", price="280000 XOF", retailer_id="SAM-A56")]
    client = FakeMetaCatalogClient(products=products)

    result = await sync_meta_catalog(db_session, tenant.id, connection, client=client)

    assert result.imported == 1
    assert connection.last_sync_status == SyncStatus.SUCCESS

    from sqlalchemy import select

    from app.models.product import Product

    product = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id))).scalars().first()
    assert product.currency == "XOF"
    assert float(product.price) == 280000.0
    assert product.external_source == "META_CATALOG"


@pytest.mark.asyncio
async def test_sync_upserts_by_meta_id_not_sku(db_session, unique_email):
    """Le retailer_id (SKU) peut changer côté Meta sans casser l'upsert, qui se fait par id Meta."""
    tenant, connection = await _setup_connection(db_session, unique_email)

    client1 = FakeMetaCatalogClient(products=[make_meta_product("1", "Produit", retailer_id="OLD-SKU")])
    await sync_meta_catalog(db_session, tenant.id, connection, client=client1)

    client2 = FakeMetaCatalogClient(products=[make_meta_product("1", "Produit renommé", retailer_id="NEW-SKU")])
    result2 = await sync_meta_catalog(db_session, tenant.id, connection, client=client2)

    assert result2.updated == 1
    assert result2.imported == 0


@pytest.mark.asyncio
async def test_sync_invalid_currency_rejected(db_session, unique_email):
    tenant, connection = await _setup_connection(db_session, unique_email)
    products = [make_meta_product("1", "Prix sans devise claire", price="1000")]  # pas de code devise
    client = FakeMetaCatalogClient(products=products)

    result = await sync_meta_catalog(db_session, tenant.id, connection, client=client)
    assert result.failed == 1
    assert result.imported == 0


@pytest.mark.asyncio
async def test_sync_connection_failure_reported_gracefully(db_session, unique_email):
    tenant, connection = await _setup_connection(db_session, unique_email)

    class BrokenClient(FakeMetaCatalogClient):
        async def fetch_products(self, limit=100, max_pages=50):
            raise RuntimeError("Token expiré")

    result = await sync_meta_catalog(db_session, tenant.id, connection, client=BrokenClient())
    assert result.total_rows == 0
    assert connection.last_sync_status == SyncStatus.FAILED
