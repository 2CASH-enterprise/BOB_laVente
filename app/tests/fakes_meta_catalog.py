class FakeMetaCatalogClient:
    def __init__(self, catalog_id=None, access_token=None, products=None, raise_on_connect=False):
        self.catalog_id = catalog_id
        self.access_token = access_token
        self._products = products or []
        self.raise_on_connect = raise_on_connect

    async def fetch_catalog_info(self) -> dict:
        if self.raise_on_connect:
            raise RuntimeError("Token invalide")
        return {"name": "Catalogue Test", "vertical": "commerce"}

    async def fetch_products(self, limit: int = 100, max_pages: int = 50) -> list[dict]:
        return self._products


def make_meta_product(
    product_id: str, name: str, price: str = "280000 XOF", retailer_id: str = "",
    availability: str = "in stock", inventory=None, image_url: str | None = "https://example.com/img.jpg",
) -> dict:
    return {
        "id": product_id,
        "retailer_id": retailer_id,
        "name": name,
        "description": f"Description de {name}",
        "price": price,
        "availability": availability,
        "image_url": image_url,
        "inventory": inventory,
    }
