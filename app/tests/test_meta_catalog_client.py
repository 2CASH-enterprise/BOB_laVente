from app.integrations.ecommerce.meta_catalog_client import map_meta_catalog_product
from app.tests.fakes_meta_catalog import make_meta_product


def test_map_meta_product_parses_price_and_currency():
    raw = make_meta_product("123", "Samsung A56", price="280000 XOF", retailer_id="SAM-A56")
    mapped = map_meta_catalog_product(raw)
    assert mapped["price"] == "280000"
    assert mapped["currency"] == "XOF"
    assert mapped["sku"] == "SAM-A56"


def test_map_meta_product_missing_retailer_id_falls_back_to_meta_id():
    raw = make_meta_product("456", "Sans SKU", retailer_id="")
    mapped = map_meta_catalog_product(raw)
    assert mapped["sku"] == "meta-456"


def test_map_meta_product_out_of_stock_is_inactive():
    raw = make_meta_product("1", "Rupture", availability="out of stock")
    mapped = map_meta_catalog_product(raw)
    assert mapped["active"] is False
    assert mapped["stock_quantity"] == 0


def test_map_meta_product_in_stock_without_inventory_field_defaults_to_one():
    """Le champ inventory est optionnel côté Meta : jamais un chiffre inventé au-delà de 1."""
    raw = make_meta_product("1", "Dispo sans quantité précisée", availability="in stock", inventory=None)
    mapped = map_meta_catalog_product(raw)
    assert mapped["stock_quantity"] == 1
    assert mapped["active"] is True


def test_map_meta_product_uses_explicit_inventory_when_present():
    raw = make_meta_product("1", "Avec inventaire précis", availability="in stock", inventory=25)
    mapped = map_meta_catalog_product(raw)
    assert mapped["stock_quantity"] == 25


def test_map_meta_product_discontinued_is_inactive():
    raw = make_meta_product("1", "Discontinué", availability="discontinued")
    mapped = map_meta_catalog_product(raw)
    assert mapped["active"] is False
