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


# --- Lot 20 : prix mis en forme par Meta, jeton jamais dans l'adresse ----------------------

import httpx  # noqa: E402
import pytest  # noqa: E402
from decimal import Decimal  # noqa: E402

from app.integrations.ecommerce.meta_catalog_client import (  # noqa: E402
    MetaCatalogClient,
    _without_token,
    describe_catalog_error,
    parse_meta_price,
)


@pytest.mark.parametrize("price, currency, expected", [
    ("280000 XOF", None, (Decimal("280000"), "XOF")),
    ("280 000 FCFA", "XOF", (Decimal("280000"), "XOF")),
    ("280 000 F CFA", "XOF", (Decimal("280000"), "XOF")),
    ("FCFA 280,000", "XOF", (Decimal("280000"), "XOF")),
    ("280.000 FCFA", "XOF", (Decimal("280000"), "XOF")),
    ("280 000,00 XOF", None, (Decimal("280000"), "XOF")),
    ("$19.99", "USD", (Decimal("19.99"), "USD")),
    ("US$1,299.50", "USD", (Decimal("1299.50"), "USD")),
    ("12,50 €", "EUR", (Decimal("12.50"), "EUR")),
    ("1.234,5 €", "EUR", (Decimal("1234.5"), "EUR")),
])
def test_meta_formatted_prices_are_read_as_written(price, currency, expected):
    assert parse_meta_price(price, currency) == expected


@pytest.mark.parametrize("price, currency", [
    ("", "XOF"), (None, "XOF"), ("Prix sur demande", "XOF"),
    ("280,5 FCFA", "XOF"),  # des décimales en francs CFA : on ne devine pas
])
def test_unreadable_prices_are_refused_rather_than_guessed(price, currency):
    assert parse_meta_price(price, currency)[0] is None


def test_cfa_alone_is_never_taken_for_a_currency_code():
    assert parse_meta_price("280 000 F CFA", None) == (Decimal("280000"), None)


def test_real_meta_shape_maps_to_a_product():
    raw = make_meta_product("9", "Robe wax", price="35 000 FCFA", retailer_id="ROBE-1")
    raw["currency"] = "XOF"
    mapped = map_meta_catalog_product(raw)
    assert (mapped["price"], mapped["currency"]) == ("35000", "XOF")


def test_token_travels_in_the_header_never_in_the_url(monkeypatch):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path.endswith("/products") and "after" not in str(request.url):
            return httpx.Response(200, json={"data": [{"id": "1"}], "paging": {
                "next": "https://graph.facebook.com/v25.0/42/products?access_token=SECRET-TOKEN&after=abc&limit=100"}})
        return httpx.Response(200, json={"data": [{"id": "2"}], "name": "Catalogue"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    client = MetaCatalogClient("42", "SECRET-TOKEN")

    import asyncio

    asyncio.run(client.fetch_catalog_info())
    products = asyncio.run(client.fetch_products())

    assert [p["id"] for p in products] == ["1", "2"]
    assert len(seen) == 3
    for request in seen:
        assert "SECRET-TOKEN" not in str(request.url)
        assert request.headers["Authorization"] == "Bearer SECRET-TOKEN"


def test_pagination_url_loses_its_token_but_keeps_the_cursor():
    url = _without_token("https://graph.facebook.com/v25.0/42/products?access_token=T0K&after=abc&limit=100")
    assert "T0K" not in url and "after=abc" in url and "limit=100" in url


def test_error_description_never_contains_the_request_url():
    request = httpx.Request("GET", "https://graph.facebook.com/v25.0/42?access_token=SECRET-TOKEN")
    response = httpx.Response(400, request=request, json={"error": {"code": 190, "message": "Invalid OAuth access token"}})
    exc = httpx.HTTPStatusError("Client error '400 Bad Request' for url '" + str(request.url) + "'", request=request, response=response)
    assert "SECRET-TOKEN" in str(exc)  # le piège : le message brut contient l'adresse
    text = describe_catalog_error(exc)
    assert "SECRET-TOKEN" not in text and "graph.facebook.com" not in text
    assert text == "Meta a répondu 400 (code 190 : Invalid OAuth access token)"
    assert describe_catalog_error(httpx.ConnectTimeout("x")) == "Meta n'a pas répondu à temps"
