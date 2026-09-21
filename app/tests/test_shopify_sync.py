import pytest

from app.models.ecommerce_connection import EcommerceConnection, EcommercePlatform, SyncStatus
from app.models.tenant import Tenant
from app.services.shopify_sync import sync_shopify_catalog
from app.tests.fakes_shopify import FakeShopifyClient, make_shopify_product


async def _setup_connection(db_session, email: str, currency="XOF") -> tuple[Tenant, EcommerceConnection]:
    tenant = Tenant(name="Boutique", country="SN", currency=currency, email=email)
    db_session.add(tenant)
    await db_session.flush()
    connection = EcommerceConnection(
        tenant_id=tenant.id, platform=EcommercePlatform.SHOPIFY, shop_domain="test.myshopify.com",
        access_token="fake-token", currency=currency,
    )
    db_session.add(connection)
    await db_session.commit()
    await db_session.refresh(connection)
    return tenant, connection


@pytest.mark.asyncio
async def test_sync_creates_new_products(db_session, unique_email):
    tenant, connection = await _setup_connection(db_session, unique_email)
    products = [
        make_shopify_product(1, "Samsung A56", "280000", sku="SAM-A56", inventory_quantity=12),
        make_shopify_product(2, "Samsung A36", "220000", sku="SAM-A36", inventory_quantity=0),
    ]
    client = FakeShopifyClient(products=products, currency="XOF")

    result = await sync_shopify_catalog(db_session, tenant.id, connection, client=client)

    assert result.total_rows == 2
    assert result.imported == 2
    assert result.updated == 0
    assert result.available == 1  # seul le A56 a du stock
    assert result.unavailable == 1
    assert connection.last_sync_status == SyncStatus.SUCCESS
    assert connection.last_synced_at is not None


@pytest.mark.asyncio
async def test_sync_upserts_on_second_run(db_session, unique_email):
    tenant, connection = await _setup_connection(db_session, unique_email)
    client1 = FakeShopifyClient(products=[make_shopify_product(1, "Samsung A56", "280000", sku="SAM-A56")], currency="XOF")
    await sync_shopify_catalog(db_session, tenant.id, connection, client=client1)

    client2 = FakeShopifyClient(products=[make_shopify_product(1, "Samsung A56", "260000", sku="SAM-A56")], currency="XOF")
    result2 = await sync_shopify_catalog(db_session, tenant.id, connection, client=client2)

    assert result2.imported == 0
    assert result2.updated == 1  # même produit Shopify (id=1), mis à jour, jamais dupliqué

    from sqlalchemy import select

    from app.models.product import Product

    products_in_db = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id))).scalars().all()
    assert len(products_in_db) == 1
    assert float(products_in_db[0].price) == 260000.0


@pytest.mark.asyncio
async def test_sync_handles_invalid_price_without_failing_others(db_session, unique_email):
    tenant, connection = await _setup_connection(db_session, unique_email)
    products = [
        make_shopify_product(1, "Produit valide", "1000", sku="OK"),
        make_shopify_product(2, "Produit invalide", "", sku="BAD"),  # prix vide
    ]
    client = FakeShopifyClient(products=products, currency="XOF")

    result = await sync_shopify_catalog(db_session, tenant.id, connection, client=client)
    assert result.imported == 1
    assert result.failed == 1
    assert len(result.errors) == 1


@pytest.mark.asyncio
async def test_sync_connection_failure_is_reported_gracefully(db_session, unique_email):
    tenant, connection = await _setup_connection(db_session, unique_email)
    client = FakeShopifyClient(raise_on_connect=True)

    # fetch_products lève aussi une erreur dans ce scénario de panne réseau simulée
    class BrokenClient(FakeShopifyClient):
        async def fetch_products(self, limit=250, max_pages=20):
            raise RuntimeError("Connexion refusée")

    result = await sync_shopify_catalog(db_session, tenant.id, connection, client=BrokenClient())
    assert result.total_rows == 0
    assert "Échec de connexion" in result.errors[0]
    assert connection.last_sync_status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_products_isolated_by_tenant(db_session, unique_email):
    tenant_a, connection_a = await _setup_connection(db_session, unique_email)
    tenant_b, connection_b = await _setup_connection(db_session, f"b_{unique_email}")

    client = FakeShopifyClient(products=[make_shopify_product(1, "Produit A", "1000", sku="A")], currency="XOF")
    await sync_shopify_catalog(db_session, tenant_a.id, connection_a, client=client)

    from sqlalchemy import select

    from app.models.product import Product

    products_b = (await db_session.execute(select(Product).where(Product.tenant_id == tenant_b.id))).scalars().all()
    assert products_b == []  # rien n'a fuité vers le tenant B
