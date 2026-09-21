"""
Client Meta Commerce Catalog (Graph API). Même principe d'isolation testable que
shopify_client.py — aucun appel réseau réel en dehors de ce module.

Un catalogue Meta peut déjà être connecté à un compte WhatsApp Business pour les
messages catalogue (section 5, catalogue produits dans la conversation) — le
récupérer ici permet à Bob de s'appuyer sur les MÊMES données produit que la
boutique Facebook/Instagram du commerce, sans double saisie.
"""
import httpx

from app.core.config import get_settings

settings = get_settings()

GRAPH_BASE_URL = "https://graph.facebook.com"

PRODUCT_FIELDS = "id,retailer_id,name,description,price,currency,availability,image_url,inventory"


class MetaCatalogClient:
    def __init__(self, catalog_id: str, access_token: str, api_version: str | None = None):
        self.catalog_id = catalog_id.strip()
        self.access_token = access_token
        self.api_version = api_version or settings.whatsapp_graph_api_version

    async def fetch_catalog_info(self) -> dict:
        """Sert de test de connexion (section 25) avant d'enregistrer quoi que ce soit."""
        url = f"{GRAPH_BASE_URL}/{self.api_version}/{self.catalog_id}"
        params = {"fields": "name,vertical", "access_token": self.access_token}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    async def fetch_products(self, limit: int = 100, max_pages: int = 50) -> list[dict]:
        """
        Suit la pagination par curseur de la Graph API (paging.next), jusqu'à max_pages
        (garde-fou contre toute boucle infinie même en cas de réponse inattendue).
        """
        products: list[dict] = []
        url = f"{GRAPH_BASE_URL}/{self.api_version}/{self.catalog_id}/products"
        params: dict | None = {"fields": PRODUCT_FIELDS, "limit": limit, "access_token": self.access_token}

        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(max_pages):
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                products.extend(data.get("data", []))

                next_url = data.get("paging", {}).get("next")
                if not next_url:
                    break
                url, params = next_url, None  # l'URL "next" contient déjà tous les paramètres

        return products


def map_meta_catalog_product(raw: dict) -> dict:
    """
    Traduit un produit du Meta Commerce Catalog vers le format interne de Bob.
    Le prix Meta est formaté "<montant> <devise>" (ex. "280000 XOF") — jamais recalculé,
    seulement reparsé tel quel.
    """
    price_raw = (raw.get("price") or "").strip()
    parts = price_raw.split()
    amount = parts[0] if parts else None
    currency = parts[1] if len(parts) > 1 else raw.get("currency")

    availability = (raw.get("availability") or "").strip().lower()
    unavailable_states = {"out of stock", "discontinued"}
    active = availability not in unavailable_states and availability != ""

    inventory = raw.get("inventory")
    if inventory is not None:
        try:
            stock_quantity = int(inventory)
        except (TypeError, ValueError):
            stock_quantity = 1 if active else 0
    else:
        # Le champ inventory est optionnel côté Meta ; à défaut, on déduit une présence
        # binaire depuis availability plutôt que d'inventer une quantité précise.
        stock_quantity = 1 if active else 0

    return {
        "external_id": str(raw.get("id")),
        "sku": raw.get("retailer_id") or f"meta-{raw.get('id')}",
        "name": raw.get("name") or "Produit sans nom",
        "description": raw.get("description"),
        "price": amount,
        "currency": currency,
        "stock_quantity": stock_quantity,
        "image_url": raw.get("image_url"),
        "active": active,
    }
