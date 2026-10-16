"""Lot 78 — fiches produits par catégorie (secteur Commerce, France d'abord ; décisions du 16/10)."""
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.prompts import SHEET_RULE, build_system_prompt
from app.agents.tools import ToolExecutor
from app.integrations.ecommerce.shopify_client import map_shopify_product
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.ecommerce_connection import EcommerceConnection, EcommercePlatform
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.services import product_sheets as ps
from app.services.business_type import BUSINESS_TYPES, CAR_DEALERSHIP, ONLINE_STORE
from app.services.catalog_overview import catalog_overview
from app.services.shopify_sync import sync_shopify_catalog
from app.tests.fakes_shopify import FakeShopifyClient
from app.tests.test_vehicles import _headers, _tenant

DASHBOARD = Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html"


# --- Catégories, fiches, complétude ---------------------------------------------------------------

def test_seven_fixed_categories_with_key_fields_and_questions():
    assert list(ps.CATEGORIES) == ["FASHION", "BEAUTY", "ELECTRONICS", "APPLIANCES", "HOME", "FOOD", "GENERAL"]
    for code, cat in ps.CATEGORIES.items():
        assert cat["questions"], code
        assert code == ps.GENERAL or 2 <= len(cat["key"]) <= 4, code


@pytest.mark.parametrize("texts, code", [
    (("Robe wax Awa",), "FASHION"),
    (("Perruque lace frontale 18 pouces",), "BEAUTY"),
    (("Chargeur USB-C 25 W",), "ELECTRONICS"),
    (("Réfrigérateur combiné 300 L",), "APPLIANCES"),
    (("Canapé 3 places",), "HOME"),
    (("Riz parfumé 5 kg",), "FOOD"),
    (("Rétroviseur Yamaha NMAX",), "GENERAL"),
    (("Sneakers", "Shoes, Men"), "FASHION"),
])
def test_category_is_guessed_by_keywords_without_ai(texts, code):
    assert ps.guess_category(*texts) == code


def test_sheet_keeps_known_fields_only_cleaned_and_bounded():
    sheet = ps.normalize_sheet("FASHION", {"sizes": " 38 à 44 ", "colors": "", "brand": "Nike", "ingredients": "x",
                                           "compare_at_price": "49,90", "material": "c" * 900, "hack": "<script>"})
    assert sheet == {"sizes": "38 à 44", "brand": "Nike", "compare_at_price": 49.9, "material": "c" * ps.MAX_VALUE}
    assert ps.normalize_sheet("FOOD", {"sizes": "M"}) is None and ps.normalize_sheet("FOOD", None) is None


def test_completeness_counts_photo_description_and_key_fields():
    p = Product(name="Robe", bob_category="FASHION", image_url="https://x/y.jpg", description="Robe d'été",
                sheet={"sizes": "S-L"})
    assert p.sheet_completeness == 60  # photo, description, tailles sur 5
    p.variants = [{"options": {"Taille": "S", "Couleur": "Bleu"}, "stock": 1}]
    assert p.sheet_completeness == 80  # Shopify : les variantes valent tailles et couleurs
    assert Product(name="x").sheet_completeness is None  # pas de catégorie : pas de note
    assert Product(name="x", bob_category="GENERAL", sheet={"features": "Compatible : NMAX"}).sheet_completeness == 33


def test_what_bob_receives_labels_and_variants():
    p = Product(name="Robe", bob_category="FASHION", sheet={"sizes": "S-L", "material": "Lin", "brand": "Awa"},
                variants=[{"options": {"Taille": "S"}, "stock": 2}, {"options": {"Taille": "M"}, "stock": 0}])
    assert ps.sheet_for_ai(p) == {"catégorie": "Mode, chaussures et accessoires", "Marque": "Awa",
                                  "Tailles ou pointures": "S-L", "Matière": "Lin",
                                  "variantes": {"disponibles": ["S"], "épuisées": ["M"]}}
    assert ps.sheet_for_ai(Product(name="x")) is None


# --- API produits ---------------------------------------------------------------------------------

BASE = {"sku": "R1", "name": "Robe wax Awa", "price": 35, "currency": "EUR", "stock_quantity": 3}


@pytest.mark.asyncio
async def test_api_guesses_category_uses_default_and_refuses_unknown(client, db_session, unique_email):
    tenant = await _tenant(db_session, unique_email, business_type=ONLINE_STORE)
    headers = await _headers(client, unique_email)

    guessed = (await client.post("/api/v1/products", json=BASE, headers=headers)).json()
    assert guessed["bob_category"] == "FASHION" and guessed["sheet_completeness"] == 0
    bad = await client.post("/api/v1/products", json={**BASE, "sku": "R2", "bob_category": "AUTRE"}, headers=headers)
    assert bad.status_code == 422

    assert (await client.put("/api/v1/tenants/me/profile", json={"default_bob_category": "NOPE"}, headers=headers)).status_code == 422
    assert (await client.put("/api/v1/tenants/me/profile", json={"default_bob_category": "BEAUTY"}, headers=headers)).status_code == 200
    by_default = (await client.post("/api/v1/products", json={**BASE, "sku": "R3", "name": "Coffret cadeau"}, headers=headers)).json()
    assert by_default["bob_category"] == "BEAUTY"
    await db_session.refresh(tenant)
    assert tenant.default_bob_category == "BEAUTY"


@pytest.mark.asyncio
async def test_api_saves_sheet_and_changing_category_keeps_common_fields(client, db_session, unique_email):
    await _tenant(db_session, unique_email, business_type=ONLINE_STORE)
    headers = await _headers(client, unique_email)
    created = (await client.post("/api/v1/products", json={**BASE, "bob_category": "FASHION",
                                                            "sheet": {"sizes": "38-44", "brand": "Awa", "x": 1}}, headers=headers)).json()
    assert created["sheet"] == {"sizes": "38-44", "brand": "Awa"}
    moved = (await client.put(f"/api/v1/products/{created['id']}", json={"bob_category": "HOME"}, headers=headers)).json()
    assert moved["bob_category"] == "HOME" and moved["sheet"] == {"brand": "Awa"}
    untouched = (await client.put(f"/api/v1/products/{created['id']}", json={"stock_quantity": 9}, headers=headers)).json()
    assert untouched["sheet"] == {"brand": "Awa"} and untouched["bob_category"] == "HOME"

    defs = (await client.get("/api/v1/products/sheets", headers=headers)).json()
    assert [c["code"] for c in defs["categories"]][0] == "FASHION" and defs["common_fields"][0]["key"] == "brand"


@pytest.mark.asyncio
async def test_dealership_products_get_no_sheet(client, db_session, unique_email):
    await _tenant(db_session, unique_email, business_type=CAR_DEALERSHIP)
    headers = await _headers(client, unique_email)
    created = (await client.post("/api/v1/products", json={**BASE, "name": "Robe", "bob_category": "FASHION",
                                                            "sheet": {"sizes": "M"}}, headers=headers)).json()
    assert created["bob_category"] is None and created["sheet"] is None


@pytest.mark.asyncio
async def test_bulk_guess_only_fills_missing_categories(client, db_session, unique_email):
    tenant = await _tenant(db_session, unique_email, business_type=ONLINE_STORE)
    headers = await _headers(client, unique_email)
    db_session.add_all([Product(tenant_id=tenant.id, sku="a", name="Chargeur iPhone", price=10, currency="EUR"),
                        Product(tenant_id=tenant.id, sku="b", name="Riz 5 kg", price=10, currency="EUR", bob_category="HOME")])
    await db_session.commit()
    assert (await client.post("/api/v1/products/sheets/guess", headers=headers)).json() == {"updated": 1}
    rows = {p.sku: p.bob_category for p in (await db_session.execute(
        select(Product).where(Product.tenant_id == tenant.id).execution_options(populate_existing=True))).scalars()}
    assert rows == {"a": "ELECTRONICS", "b": "HOME"}  # le choix du commerçant n'est jamais écrasé


# --- Bob -----------------------------------------------------------------------------------------

async def _shop(db_session, email):
    tenant = Tenant(name="Boutique Awa", country="FR", currency="EUR", email=email, plan=TenantPlan.PRO,
                    business_type=ONLINE_STORE)
    db_session.add(tenant)
    await db_session.flush()
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"336{uuid.uuid4().int % 10**8:08d}")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id)
    db_session.add(conversation)
    await db_session.commit()
    return tenant, conversation


@pytest.mark.asyncio
async def test_bob_sees_the_sheet_and_the_questions_of_present_categories(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    db_session.add(Product(tenant_id=tenant.id, sku="R", name="Robe wax", price=35, currency="EUR", stock_quantity=2,
                           bob_category="FASHION", sheet={"sizes": "38-44"},
                           variants=[{"options": {"Taille": "38"}, "stock": 2}, {"options": {"Taille": "40"}, "stock": 0}]))
    await db_session.commit()
    result = await ToolExecutor(db_session, tenant.id, conversation, business_type=ONLINE_STORE).execute(
        "search_products", {"query": "Robe"})
    fiche = result["results"][0]["fiche"]
    assert fiche["Tailles ou pointures"] == "38-44" and fiche["variantes"]["épuisées"] == ["40"]

    overview = await catalog_overview(db_session, tenant.id)
    assert "QUESTIONS UTILES PAR CATÉGORIE" in overview and "Quelle taille ou pointure portez-vous ?" in overview
    assert "Alimentation" not in overview  # seulement les catégories présentes


def test_sheet_rule_is_for_shops_only():
    shop = Tenant(name="B", country="FR", currency="EUR", business_type=ONLINE_STORE)
    car = Tenant(name="C", country="FR", currency="EUR", business_type=CAR_DEALERSHIP)
    assert SHEET_RULE in build_system_prompt(shop) and SHEET_RULE not in build_system_prompt(car)
    assert "un champ absent = « je n'ai pas cette information »" in SHEET_RULE


# --- Shopify : toutes les variantes, marque, prix barré, catégorie — sans IA -----------------------

def _shopify(pid=1, vendor="Awa", product_type="Robes", tags="été, wax"):
    return {"id": pid, "title": "Robe Awa", "body_html": "<p>Lin</p>", "status": "active", "product_type": product_type,
            "vendor": vendor, "tags": tags, "images": [{"src": "https://cdn.shopify.com/r.jpg"}],
            "options": [{"name": "Taille", "position": 1}, {"name": "Couleur", "position": 2}],
            "variants": [
                {"sku": "R-38-B", "price": "35.00", "compare_at_price": "49.00", "inventory_quantity": 2,
                 "option1": "38", "option2": "Bleu", "barcode": "3700000000001"},
                {"sku": "R-40-B", "price": "35.00", "inventory_quantity": 0, "option1": "40", "option2": "Bleu"},
                {"sku": "R-42-V", "price": "35.00", "inventory_quantity": -1, "option1": "42", "option2": "Vert"},
            ]}


def test_shopify_mapping_reads_every_variant():
    mapped = map_shopify_product(_shopify())
    assert mapped["stock_quantity"] == 2  # somme des variantes en stock (avant : la première seulement)
    assert [v["options"] for v in mapped["variants"]] == [{"Taille": "38", "Couleur": "Bleu"}, {"Taille": "40", "Couleur": "Bleu"},
                                                          {"Taille": "42", "Couleur": "Vert"}]
    assert mapped["sheet"] == {"brand": "Awa", "compare_at_price": 49.0, "ean": "3700000000001"}
    assert mapped["bob_category"] == "FASHION"
    single = map_shopify_product({"id": 2, "title": "Bougie", "variants": [{"price": "9", "inventory_quantity": 4,
                                                                            "option1": "Default Title"}],
                                  "options": [{"name": "Title", "position": 1}]})
    assert single["variants"] is None and single["stock_quantity"] == 4


async def _connection(db_session, email, business_type=ONLINE_STORE):
    tenant = Tenant(name="Boutique", country="FR", currency="EUR", email=email, business_type=business_type)
    db_session.add(tenant)
    await db_session.flush()
    connection = EcommerceConnection(tenant_id=tenant.id, platform=EcommercePlatform.SHOPIFY,
                                     shop_domain="awa.myshopify.com", access_token="t", currency="EUR")
    db_session.add(connection)
    await db_session.commit()
    return tenant, connection


@pytest.mark.asyncio
async def test_shopify_sync_fills_sheets_but_never_overrides_the_merchant(db_session, unique_email):
    tenant, connection = await _connection(db_session, unique_email)
    await sync_shopify_catalog(db_session, tenant.id, connection, client=FakeShopifyClient(products=[_shopify()]))
    product = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id))).scalar_one()
    assert product.bob_category == "FASHION" and product.sheet["brand"] == "Awa" and len(product.variants) == 3

    product.bob_category = "BEAUTY"   # le commerçant corrige
    product.sheet = {"brand": "Awa", "ingredients": "Karité"}
    await db_session.commit()
    await sync_shopify_catalog(db_session, tenant.id, connection,
                               client=FakeShopifyClient(products=[_shopify(vendor="Awa Paris")]))
    await db_session.refresh(product)
    assert product.bob_category == "BEAUTY" and product.sheet == {"brand": "Awa Paris", "ingredients": "Karité",
                                                                  "compare_at_price": 49.0, "ean": "3700000000001"}


@pytest.mark.asyncio
async def test_shopify_sync_of_a_dealership_adds_no_sheet(db_session, unique_email):
    tenant, connection = await _connection(db_session, unique_email, business_type=CAR_DEALERSHIP)
    await sync_shopify_catalog(db_session, tenant.id, connection, client=FakeShopifyClient(products=[_shopify()]))
    product = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id))).scalar_one()
    assert product.bob_category is None and product.sheet is None and product.variants is None


@pytest.mark.asyncio
async def test_nightly_shopify_sync_goes_through_every_shop(db_session, unique_email):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.workers.catalog_sync import _sync_all_shopify

    await _connection(db_session, unique_email)
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False, class_=type(db_session))
    report = await _sync_all_shopify(factory, client_factory=lambda d, t: FakeShopifyClient(products=[_shopify()]))
    assert report["synced"] >= 1 and report["failed"] == 0


def test_dealership_is_now_auto_and_moto():
    assert BUSINESS_TYPES[CAR_DEALERSHIP]["label"] == "Concession auto / moto"


def test_dashboard_has_category_sheet_and_completeness():
    html = DASHBOARD.read_text(encoding="utf-8")
    for needle in ('id="new-product-bob-category"', 'id="default-bob-category"', "function sheetFields(",
                   "/api/v1/products/sheets/guess", "fiche ${p.sheet_completeness} %", "Plus de détails (facultatif)"):
        assert needle in html


@pytest.mark.asyncio
async def test_shopify_fields_removed_in_shopify_are_removed_in_bob(db_session, unique_email):
    tenant, connection = await _connection(db_session, unique_email)
    await sync_shopify_catalog(db_session, tenant.id, connection, client=FakeShopifyClient(products=[_shopify()]))
    no_promo = _shopify(vendor="")
    no_promo["variants"][0].pop("compare_at_price")
    await sync_shopify_catalog(db_session, tenant.id, connection, client=FakeShopifyClient(products=[no_promo]))
    product = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id)
                                        .execution_options(populate_existing=True))).scalar_one()
    assert product.sheet == {"ean": "3700000000001"}  # plus de marque ni de prix barré : la promotion est finie
