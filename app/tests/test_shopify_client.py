from app.integrations.ecommerce.shopify_client import _parse_next_link, map_shopify_product
from app.tests.fakes_shopify import make_shopify_product


def test_map_shopify_product_basic_fields():
    raw = make_shopify_product(123, "Samsung A56", "280000.00", sku="SAM-A56", inventory_quantity=12)
    mapped = map_shopify_product(raw)
    assert mapped["external_id"] == "123"
    assert mapped["sku"] == "SAM-A56"
    assert mapped["name"] == "Samsung A56"
    assert mapped["price"] == "280000.00"
    assert mapped["stock_quantity"] == 12
    assert mapped["category"] == "Téléphones"
    assert mapped["active"] is True


def test_map_shopify_product_missing_sku_falls_back_to_id():
    raw = make_shopify_product(456, "Produit sans SKU", "1000", sku="")
    mapped = map_shopify_product(raw)
    assert mapped["sku"] == "shopify-456"


def test_map_shopify_product_no_images_gives_none():
    raw = make_shopify_product(789, "Produit sans image", "1000", image_url=None)
    mapped = map_shopify_product(raw)
    assert mapped["image_url"] is None


def test_map_shopify_product_draft_status_is_inactive():
    raw = make_shopify_product(1, "Brouillon", "1000", status="draft")
    mapped = map_shopify_product(raw)
    assert mapped["active"] is False


def test_parse_next_link_extracts_url():
    header = '<https://shop.myshopify.com/admin/api/2024-10/products.json?page_info=abc>; rel="next"'
    assert _parse_next_link(header) == "https://shop.myshopify.com/admin/api/2024-10/products.json?page_info=abc"


def test_parse_next_link_with_prev_and_next():
    header = (
        '<https://x/products.json?page_info=prev>; rel="previous", '
        '<https://x/products.json?page_info=next>; rel="next"'
    )
    assert _parse_next_link(header) == "https://x/products.json?page_info=next"


def test_parse_next_link_none_when_absent():
    assert _parse_next_link(None) is None
    assert _parse_next_link('<https://x/products.json?page_info=prev>; rel="previous"') is None
