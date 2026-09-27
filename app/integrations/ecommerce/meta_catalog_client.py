"""
Client Meta Commerce Catalog (Graph API). Même principe d'isolation testable que
shopify_client.py — aucun appel réseau réel en dehors de ce module.

Un catalogue Meta peut déjà être connecté à un compte WhatsApp Business pour les
messages catalogue (section 5, catalogue produits dans la conversation) — le
récupérer ici permet à Bob de s'appuyer sur les MÊMES données produit que la
boutique Facebook/Instagram du commerce, sans double saisie.
"""
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.core.config import get_settings

settings = get_settings()

GRAPH_BASE_URL = "https://graph.facebook.com"

PRODUCT_FIELDS = "id,retailer_id,name,description,price,currency,availability,image_url,inventory"


def describe_catalog_error(exc: Exception) -> str:
    """
    Motif lisible d'un échec, SANS l'adresse de la requête : le message brut de httpx
    contient l'URL complète (lot 20 : le jeton y figurait). Code et message Meta seulement.
    """
    from app.integrations.whatsapp.client import MetaGraphError

    if isinstance(exc, MetaGraphError):
        return str(exc)  # déjà décrite sans adresse ni secret
    if isinstance(exc, httpx.HTTPStatusError):
        from app.integrations.whatsapp.client import describe_meta_error

        return f"Meta a répondu {exc.response.status_code} ({describe_meta_error(exc.response)})"
    if isinstance(exc, httpx.TimeoutException):
        return "Meta n'a pas répondu à temps"
    if isinstance(exc, httpx.HTTPError):
        return "Meta est injoignable pour le moment"
    return type(exc).__name__


def _without_token(url: str) -> str:
    """Retire un éventuel access_token d'une URL de pagination : le jeton voyage dans l'en-tête."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "access_token"]
    return urlunsplit(parts._replace(query=urlencode(query)))


class MetaCatalogClient:
    def __init__(self, catalog_id: str, access_token: str, api_version: str | None = None):
        self.catalog_id = catalog_id.strip()
        self.access_token = access_token
        self.api_version = api_version or settings.whatsapp_graph_api_version

    async def fetch_catalog_info(self) -> dict:
        """Sert de test de connexion (section 25) avant d'enregistrer quoi que ce soit."""
        url = f"{GRAPH_BASE_URL}/{self.api_version}/{self.catalog_id}"
        params = {"fields": "name,vertical"}
        async with httpx.AsyncClient(timeout=15, headers=self._headers()) as client:
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
        params: dict | None = {"fields": PRODUCT_FIELDS, "limit": limit}

        async with httpx.AsyncClient(timeout=30, headers=self._headers()) as client:
            for _ in range(max_pages):
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                products.extend(data.get("data", []))

                next_url = data.get("paging", {}).get("next")
                if not next_url:
                    break
                url, params = _without_token(next_url), None  # l'URL "next" contient déjà les paramètres

        return products

    def _headers(self) -> dict:
        # Jamais dans l'adresse (qui finit dans les messages d'erreur et les journaux).
        return {"Authorization": f"Bearer {self.access_token}"}


# Devises sans décimales : « 280.000 » y est toujours un séparateur de milliers.
_ZERO_DECIMAL = {"XOF", "XAF", "GNF", "RWF", "UGX", "KMF", "DJF", "BIF", "JPY", "KRW", "VND", "CLP", "PYG", "ISK"}
_AMOUNT = re.compile(r"\d[\d\s.,\u00a0\u202f']*")


def parse_meta_price(price: str | None, currency: str | None) -> tuple[Decimal | None, str | None]:
    """
    Meta renvoie un prix MIS EN FORME pour l'affichage (« 280 000 FCFA », « FCFA 280,000 »,
    « $19.99 », « 280000 XOF »…) et la devise séparément. Montant et devise sont relus tels
    quels, jamais recalculés ; en cas de doute, (None, …) plutôt qu'un prix deviné.
    """
    text = (price or "").strip()
    code = (currency or "").strip().upper()
    if len(code) != 3:
        found = [c for c in re.findall(r"\b([A-Z]{3})\b", text) if c != "CFA"]  # « F CFA » : ni XOF ni XAF
        code = found[0] if found else ""
    match = _AMOUNT.search(text)
    if not match:
        return None, code or None
    raw = re.sub(r"[\s\u00a0\u202f']", "", match.group(0)).rstrip(".,")
    last = max(raw.rfind("."), raw.rfind(","))
    decimals = ""
    if last != -1 and 1 <= len(raw) - last - 1 <= 2:
        decimals = raw[last + 1:]
        raw = raw[:last]
    digits = re.sub(r"[.,]", "", raw)
    if code in _ZERO_DECIMAL and decimals.strip("0"):
        return None, code or None  # « 280,5 » en francs CFA : illisible, on ne devine pas
    try:
        amount = Decimal(f"{digits}.{decimals}" if decimals and code not in _ZERO_DECIMAL else digits)
    except InvalidOperation:
        return None, code or None
    return amount, code or None


def map_meta_catalog_product(raw: dict) -> dict:
    """
    Traduit un produit du Meta Commerce Catalog vers le format interne de Bob.
    Le prix est relu depuis le texte mis en forme par Meta (parse_meta_price), jamais recalculé.
    """
    amount, currency = parse_meta_price(raw.get("price"), raw.get("currency"))

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
        "price": str(amount) if amount is not None else None,
        "price_raw": raw.get("price"),
        "currency": currency,
        "stock_quantity": stock_quantity,
        "image_url": raw.get("image_url"),
        "active": active,
    }


class MetaCatalogOAuth:
    """
    Lot 21 — connexion « en un clic » (Facebook Login for Business, permission catalog_management).
    Le navigateur ne reçoit qu'un code à usage unique ; l'échange contre le jeton et la lecture des
    catalogues réellement partagés par le commerçant se font ici, côté serveur.
    """

    def __init__(self, app_id: str, app_secret: str, api_version: str | None = None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_version = api_version or settings.whatsapp_graph_api_version

    async def exchange_code(self, code: str) -> str:
        from app.integrations.whatsapp.client import MetaOAuthClient

        return await MetaOAuthClient(self.app_id, self.app_secret, self.api_version).exchange_code_for_token(code)

    async def granted_catalogs(self, access_token: str) -> tuple[list[dict], dict]:
        """
        Catalogues que le commerçant a partagés, lus à trois sources (constat du 27/09 : avec un
        jeton « utilisateur système » d'entreprise cliente, debug_token ne donne aucune liste ;
        les catalogues sont ATTRIBUÉS à l'utilisateur système créé par Meta) :
          1. autorisations du jeton (debug_token → granular_scopes) ;
          2. catalogues attribués à l'utilisateur système (/me/assigned_product_catalogs) ;
          3. catalogues de l'entreprise cliente (/{client_business_id}/owned_product_catalogs).
        Une source en échec est ignorée. Retourne ([{id, name|None}], comptage par source) ; le
        comptage sert au diagnostic, sans jamais aucun jeton.
        """
        from app.integrations.whatsapp.client import _raise_for_meta

        found: dict[str, str | None] = {}
        counts = {"autorisations": 0, "attribues": 0, "entreprise": 0}
        base = f"{GRAPH_BASE_URL}/{self.api_version}"
        app_auth = {"Authorization": f"Bearer {self.app_id}|{self.app_secret}"}
        user_auth = {"Authorization": f"Bearer {access_token}"}

        async with httpx.AsyncClient(timeout=15) as client:
            try:
                response = await client.get(f"{base}/debug_token", params={"input_token": access_token}, headers=app_auth)
                _raise_for_meta(response)
                for scope in ((response.json() or {}).get("data") or {}).get("granular_scopes") or []:
                    if scope.get("scope") == "catalog_management":
                        for target in scope.get("target_ids") or []:
                            found.setdefault(str(target), None)
                            counts["autorisations"] += 1
            except Exception:  # noqa: BLE001
                pass

            try:
                response = await client.get(f"{base}/me/assigned_product_catalogs",
                                            params={"fields": "id,name", "limit": 50}, headers=user_auth)
                _raise_for_meta(response)
                for item in (response.json() or {}).get("data") or []:
                    if item.get("id"):
                        found[str(item["id"])] = item.get("name") or found.get(str(item["id"]))
                        counts["attribues"] += 1
            except Exception:  # noqa: BLE001
                pass

            if not found:
                try:
                    response = await client.get(f"{base}/me", params={"fields": "client_business_id"}, headers=user_auth)
                    _raise_for_meta(response)
                    business_id = (response.json() or {}).get("client_business_id")
                    if business_id:
                        response = await client.get(f"{base}/{business_id}/owned_product_catalogs",
                                                    params={"fields": "id,name", "limit": 50}, headers=user_auth)
                        _raise_for_meta(response)
                        for item in (response.json() or {}).get("data") or []:
                            if item.get("id"):
                                found[str(item["id"])] = item.get("name")
                                counts["entreprise"] += 1
                except Exception:  # noqa: BLE001
                    pass

        return [{"id": cid, "name": name} for cid, name in found.items()], counts


def get_meta_catalog_oauth() -> MetaCatalogOAuth:
    return MetaCatalogOAuth(settings.whatsapp_app_id, settings.whatsapp_app_secret)
