"""
Client Shopify Admin API (REST). Isolé du reste pour rester mockable en test,
même principe que app/integrations/whatsapp/client.py et app/agents/llm_client.py.
"""
import httpx


def _parse_next_link(link_header: str | None) -> str | None:
    """Shopify pagine via l'en-tête HTTP Link (RFC 5988), pas via un paramètre offset."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segments = [s.strip() for s in part.split(";")]
        if len(segments) < 2:
            continue
        if segments[1] == 'rel="next"':
            return segments[0].strip("<>")
    return None


class ShopifyClient:
    def __init__(self, shop_domain: str, access_token: str, api_version: str = "2024-10"):
        self.shop_domain = shop_domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
        self.access_token = access_token
        self.api_version = api_version

    def _headers(self) -> dict:
        return {"X-Shopify-Access-Token": self.access_token}

    async def fetch_shop_currency(self) -> str:
        """Utilisé aussi comme test de connexion lors de POST /integrations/shopify/connect."""
        url = f"https://{self.shop_domain}/admin/api/{self.api_version}/shop.json"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, headers=self._headers())
            response.raise_for_status()
            return response.json()["shop"]["currency"]

    async def fetch_products(self, limit: int = 250, max_pages: int = 20) -> list[dict]:
        """
        Récupère l'intégralité du catalogue, en suivant la pagination Shopify.
        max_pages est un garde-fou (jamais de boucle infinie même en cas de réponse inattendue).
        """
        products: list[dict] = []
        url = f"https://{self.shop_domain}/admin/api/{self.api_version}/products.json"
        params: dict | None = {"limit": limit}

        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(max_pages):
                response = await client.get(url, headers=self._headers(), params=params)
                response.raise_for_status()
                data = response.json()
                products.extend(data.get("products", []))

                next_url = _parse_next_link(response.headers.get("Link"))
                if not next_url:
                    break
                url, params = next_url, None  # l'URL "next" contient déjà tous les paramètres

        return products


def map_shopify_product(shopify_product: dict) -> dict:
    """
    Traduit un produit Shopify vers le format interne de Bob. Ne fait jamais confiance
    à un champ absent : toute valeur manquante reçoit un repli explicite et sûr.

    Lot 78 — toutes les variantes (options Taille / Couleur… et LEUR stock), la marque (vendor), le prix barré
    (compare_at_price), le code-barres et la catégorie Bob proposée par mots-clés : sans IA, sans coût.
    """
    from app.services.product_sheets import guess_category

    variants = shopify_product.get("variants") or [{}]
    variant = variants[0]
    images = shopify_product.get("images") or []
    option_names = {int(o.get("position") or i + 1): (o.get("name") or f"Option {i + 1}")
                    for i, o in enumerate(shopify_product.get("options") or [])}

    sku = (variant.get("sku") or "").strip() or f"shopify-{shopify_product.get('id')}"
    price = variant.get("price")

    mapped_variants = []
    for v in variants:
        options = {}
        for position in (1, 2, 3):
            value = v.get(f"option{position}")
            name = option_names.get(position, f"Option {position}")
            if value and not (value == "Default Title" and name == "Title"):
                options[name] = value
        try:
            v_price = float(v["price"]) if v.get("price") not in (None, "") else None
        except (TypeError, ValueError):
            v_price = None
        mapped_variants.append({"options": options, "stock": int(v.get("inventory_quantity") or 0),
                                "sku": (v.get("sku") or "").strip() or None, "price": v_price})
    has_options = any(mv["options"] for mv in mapped_variants)

    sheet = {}
    if (shopify_product.get("vendor") or "").strip():
        sheet["brand"] = shopify_product["vendor"].strip()
    try:
        compare_at = float(variant.get("compare_at_price") or 0)
        if price is not None and compare_at > float(price):
            sheet["compare_at_price"] = round(compare_at, 2)
    except (TypeError, ValueError):
        pass
    if (variant.get("barcode") or "").strip():
        sheet["ean"] = variant["barcode"].strip()

    tags = shopify_product.get("tags")
    tags = ", ".join(tags) if isinstance(tags, list) else (tags or "")
    title = shopify_product.get("title") or "Produit sans nom"

    return {
        "external_id": str(shopify_product.get("id")),
        "sku": sku,
        "name": title,
        "description": shopify_product.get("body_html"),
        "price": price,
        # Lot 78 : le stock du produit = la somme de ses variantes (avant : la première seulement).
        "stock_quantity": sum(mv["stock"] for mv in mapped_variants if mv["stock"] > 0) if has_options
        else int(variant.get("inventory_quantity") or 0),
        "category": shopify_product.get("product_type") or None,
        "image_url": images[0]["src"] if images else None,
        "active": shopify_product.get("status") == "active",
        "variants": mapped_variants if has_options else None,
        "sheet": sheet,
        "bob_category": guess_category(shopify_product.get("product_type"), tags, title),
    }
