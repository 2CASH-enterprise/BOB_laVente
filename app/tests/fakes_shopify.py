class FakeShopifyClient:
    def __init__(self, shop_domain=None, access_token=None, products=None, currency="EUR", raise_on_connect=False):
        self.shop_domain = shop_domain
        self.access_token = access_token
        self._products = products or []
        self._currency = currency
        self.raise_on_connect = raise_on_connect

    async def fetch_shop_currency(self) -> str:
        if self.raise_on_connect:
            raise RuntimeError("Identifiants invalides")
        return self._currency

    async def fetch_products(self, limit: int = 250, max_pages: int = 20) -> list[dict]:
        return self._products


def make_shopify_product(
    product_id: int, title: str, price: str, sku: str = "", inventory_quantity: int = 0,
    status: str = "active", product_type: str = "Téléphones", image_url: str | None = "https://example.com/img.jpg",
) -> dict:
    return {
        "id": product_id,
        "title": title,
        "body_html": f"<p>{title}</p>",
        "status": status,
        "product_type": product_type,
        "variants": [{"sku": sku, "price": price, "inventory_quantity": inventory_quantity}],
        "images": [{"src": image_url}] if image_url else [],
    }
