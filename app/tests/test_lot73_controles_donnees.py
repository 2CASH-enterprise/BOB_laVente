"""
Lot 73 — renforcement technique : contrôles des données reçues (adresse Shopify, identifiant de catalogue Meta,
catégorie parente de la même boutique sans boucle, commande pour un client de la même boutique).
"""
import uuid

import pytest
from pydantic import ValidationError

from app.core.security import hash_password
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.schemas.ecommerce import MetaCatalogConnectRequest, ShopifyConnectRequest, clean_catalog_id, clean_shop_domain
from app.services.order_service import OrderCreationError, create_order


# --- Adresse Shopify et catalogue Meta ------------------------------------------------------------------------------

def test_shop_domain_is_cleaned_and_checked():
    assert clean_shop_domain(" https://Ma-Boutique.myshopify.com/admin?x=1 ") == "ma-boutique.myshopify.com"
    assert ShopifyConnectRequest(shop_domain="test.myshopify.com", access_token="t").shop_domain == "test.myshopify.com"
    for bad in ["evil.com", "ma-boutique.myshopify.com.evil.com", "ma boutique.myshopify.com",
                "-x.myshopify.com", "x.myshopify.com@evil.com", "127.0.0.1", "a" * 70 + ".myshopify.com"]:
        with pytest.raises(ValidationError, match="Adresse Shopify invalide"):
            ShopifyConnectRequest(shop_domain=bad, access_token="t")


def test_catalog_id_must_be_digits():
    assert clean_catalog_id(" 123456789012 ") == "123456789012"
    assert MetaCatalogConnectRequest(catalog_id="987654321098", access_token="t").catalog_id == "987654321098"
    for bad in ["catalog_123", "123/../me", "1234", "12345678901234567890123456", "12 34567"]:
        with pytest.raises(ValidationError, match="Identifiant de catalogue invalide"):
            MetaCatalogConnectRequest(catalog_id=bad, access_token="t")


def test_clients_refuse_bad_values_too():
    from app.integrations.ecommerce.meta_catalog_client import MetaCatalogClient
    from app.integrations.ecommerce.shopify_client import ShopifyClient

    with pytest.raises(ValueError):
        ShopifyClient("evil.com/x", "t")
    with pytest.raises(ValueError):
        MetaCatalogClient("../me", "t")


async def _owner(db_session, email):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    await db_session.commit()
    return tenant


async def _login(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_connect_endpoint_answers_400_with_a_clear_message(client, db_session, unique_email):
    await _owner(db_session, unique_email)
    headers = await _login(client, unique_email)
    r = await client.post("/api/v1/integrations/shopify/connect", headers=headers,
                          json={"shop_domain": "evil.com", "access_token": "tok"})
    assert r.status_code in (400, 422) and "Adresse Shopify invalide" in r.text
    r = await client.post("/api/v1/integrations/meta-catalog/connect", headers=headers,
                          json={"catalog_id": "abc", "access_token": "tok"})
    assert r.status_code in (400, 422) and "Identifiant de catalogue invalide" in r.text


# --- Catégories --------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_category_parent_must_be_ours_and_never_loop(client, db_session, unique_email):
    await _owner(db_session, unique_email)
    other = f"o{uuid.uuid4().hex[:6]}@x.ci"
    await _owner(db_session, other)
    mine, theirs = await _login(client, unique_email), await _login(client, other)

    foreign = (await client.post("/api/v1/categories", headers=theirs, json={"name": "Chez l'autre"})).json()
    r = await client.post("/api/v1/categories", headers=mine, json={"name": "Téléphones", "parent_id": foreign["id"]})
    assert r.status_code == 400 and r.json()["detail"] == "Catégorie parente introuvable"

    a = (await client.post("/api/v1/categories", headers=mine, json={"name": "A"})).json()
    r = await client.post("/api/v1/categories", headers=mine, json={"name": "A", "parent_id": a["id"]})
    assert r.status_code == 400 and "propre parente" in r.json()["detail"]

    b = await client.post("/api/v1/categories", headers=mine, json={"name": "B", "parent_id": a["id"]})
    assert b.status_code == 201 and b.json()["parent_id"] == a["id"]
    c = await client.post("/api/v1/categories", headers=mine, json={"name": "C", "parent_id": b.json()["id"]})
    assert c.status_code == 201
    loop = await client.post("/api/v1/categories", headers=mine, json={"name": "A", "parent_id": c.json()["id"]})
    assert loop.status_code == 400 and "boucle" in loop.json()["detail"]
    listing = {x["name"]: x for x in (await client.get("/api/v1/categories", headers=mine)).json()}
    assert listing["A"]["parent_id"] is None  # rien n'a été modifié


# --- Commandes ---------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_order_needs_a_customer_of_the_same_shop(db_session, unique_email):
    tenant = await _owner(db_session, unique_email)
    other = await _owner(db_session, f"o{uuid.uuid4().hex[:6]}@x.ci")
    product = Product(tenant_id=tenant.id, sku="P1", name="Pagne", price=1000, currency="XOF", stock_quantity=5)
    foreign_customer = Customer(tenant_id=other.id, whatsapp_number="2250700000001")
    foreign_product = Product(tenant_id=other.id, sku="P2", name="Autre", price=1000, currency="XOF", stock_quantity=5)
    db_session.add_all([product, foreign_customer, foreign_product])
    await db_session.commit()
    product_id, foreign_customer_id, foreign_product_id = product.id, foreign_customer.id, foreign_product.id
    tenant_id = tenant.id

    with pytest.raises(OrderCreationError, match="Client introuvable"):
        await create_order(db=db_session, tenant_id=tenant_id, customer_id=foreign_customer_id,
                           items=[{"product_id": str(product_id), "quantity": 1}], delivery_address="Abidjan",
                           payment_method="cash", created_by="IA")
    await db_session.rollback()
    with pytest.raises(OrderCreationError, match="Client introuvable"):
        await create_order(db=db_session, tenant_id=tenant_id, customer_id=uuid.uuid4(),
                           items=[{"product_id": str(product_id), "quantity": 1}], delivery_address="Abidjan",
                           payment_method="cash", created_by="IA")
    await db_session.rollback()
    mine = Customer(tenant_id=tenant_id, whatsapp_number="2250700000002")
    db_session.add(mine)
    await db_session.commit()
    await db_session.refresh(mine)
    with pytest.raises(OrderCreationError):
        await create_order(db=db_session, tenant_id=tenant_id, customer_id=mine.id,
                           items=[{"product_id": str(foreign_product_id), "quantity": 1}], delivery_address="Abidjan",
                           payment_method="cash", created_by="IA")
