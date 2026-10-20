"""
Lot 76 — cartes produits sur WhatsApp (choix du 16/10 : 1A 2A 3A 4A).

- Un produit : une carte (photo, texte court, bouton « Ça m'intéresse ») ; 2 ou 3 produits : un carrousel.
- PAS de prix sur la carte (1A) : il arrive quand le client touche « Ça m'intéresse » (règle « prix sur demande »).
- 3 cartes au plus (2A), un seul bouton (3A). Bob les envoie quand il présente des produits ou qu'on lui demande
  une photo (4A). WhatsApp exige une photo par carte : un produit sans photo reste dans le texte de Bob.
- Si WhatsApp refuse la carte, les photos partent comme avant (photo + légende).
- Le bouton porte l'identifiant du produit : la réponse du client arrive à Bob sous la forme
  « Ça m'intéresse : <nom du produit> », vérifiée dans le catalogue de CETTE boutique.
"""
import logging
import re
import uuid

logger = logging.getLogger(__name__)

MAX_CARDS = 10  # lot 86 (3 avant) ; limite de WhatsApp pour un carrousel
BUTTON_TITLE = "Ça m'intéresse"
INTEREST_PREFIX = "interest:"
CARD_BODY_MAX = 160


def card_body(product) -> str:
    """Nom du produit + un détail utile tiré de la fiche (jamais le prix)."""
    from app.services.product_sheets import CATEGORIES

    detail = None
    variants = getattr(product, "variants", None) or []
    if variants:
        first = next((v.get("options") for v in variants if v.get("options")), None)
        if first:
            option = next(iter(first))
            values = []
            for v in variants:
                value = (v.get("options") or {}).get(option)
                if value and (v.get("stock") or 0) > 0 and value not in values:
                    values.append(value)
            if values:
                detail = f"{option} : {', '.join(values)}"
    sheet = getattr(product, "sheet", None) or {}
    from app.services.product_sheets import is_property

    if detail is None and is_property(getattr(product, "bob_category", None)):  # lot 79 : un bien immobilier
        from app.services.real_estate import card_detail

        detail = card_detail(product)
    if detail is None and getattr(product, "bob_category", None) in CATEGORIES:
        for key, label, _ in CATEGORIES[product.bob_category]["key"]:
            if sheet.get(key):
                detail = f"{label} : {sheet[key]}"
                break
    if detail is None and getattr(product, "vehicle", None):
        from app.services.vehicle import summary

        detail = summary(product.vehicle) or None
    if detail is None and sheet.get("brand"):
        detail = f"Marque : {sheet['brand']}"
    text = product.name if not detail else f"{product.name}\n{detail}"
    return text[:CARD_BODY_MAX]


_PRICE = re.compile(r"\d[\d\s.,\u202f\u00a0]*\s?(€|eur\b|euros?\b|xof\b|fcfa\b|cfa\b|f\b|\$|usd\b)", re.IGNORECASE)
_LIST_ITEM = re.compile(r"^\s*(?:[•\-*–]|\d+[.)])\s+")
DEFAULT_INTRO = "Voici ce que nous avons :"


def text_for_cards(text: str, names: list[str]) -> str:
    """Lot 76 — test réel du 16/10 : avant le carrousel, Bob envoyait une liste avec les prix. Quand des cartes partent,
    les lignes de liste qui nomment ces produits, et toute ligne avec un prix, sont retirées ; restent l'introduction et
    la question (une phrase qui nomme un seul produit, sans prix, reste)."""
    lowered_names = [n.lower() for n in names if n]
    kept = []
    for line in (text or "").splitlines():
        low = line.lower()
        is_item = bool(_LIST_ITEM.match(line))
        if _PRICE.search(line) or (is_item and any(n in low for n in lowered_names)):
            continue
        kept.append(line)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return cleaned or DEFAULT_INTRO


def carousel_intro(tu: bool, noun: str = "produit") -> str:
    """Lot 79 : « le bien » pour une agence immobilière, « le véhicule » pour une concession."""
    return (f"Touche « Ça m'intéresse » sous le {noun} qui te plaît." if tu
            else f"Touchez « Ça m'intéresse » sous le {noun} qui vous plaît.")


def card_noun(tenant) -> str:
    from app.services.business_type import is_dealership, is_real_estate

    return "bien" if is_real_estate(tenant) else ("véhicule" if is_dealership(tenant) else "produit")


def button_id(product_id: str) -> str:
    return f"{INTEREST_PREFIX}{product_id}"


async def interest_text(db, tenant_id, reply_id: str | None) -> str | None:
    """Réponse « Ça m'intéresse » → texte pour Bob avec le nom EXACT du produit (de cette boutique seulement)."""
    if not reply_id or not reply_id.startswith(INTEREST_PREFIX):
        return None
    from app.models.product import Product

    try:
        product_id = uuid.UUID(reply_id[len(INTEREST_PREFIX):])
    except ValueError:
        return None
    product = await db.get(Product, product_id)
    if product is None or product.tenant_id != tenant_id:
        return None
    return f"{BUTTON_TITLE} : {product.name}"


async def send(wa_client, to: str, items: list[dict], tu: bool, noun: str = "produit") -> tuple[str, list[dict]]:
    """Envoie les cartes ; renvoie (« cards » ou « images », éléments réellement partis)."""
    items = items[:MAX_CARDS]
    if not items:
        return "cards", []
    try:
        if len(items) == 1:
            item = items[0]
            await wa_client.send_product_card(to=to, link=item["link"], body=item["card"],
                                              button_id=button_id(item["product_id"]), button_title=BUTTON_TITLE)
        else:
            await wa_client.send_product_carousel(to=to, body=carousel_intro(tu, noun), cards=[
                {"link": i["link"], "body": i["card"], "button_id": button_id(i["product_id"]), "button_title": BUTTON_TITLE}
                for i in items])
        return "cards", items
    except Exception:  # noqa: BLE001 — carte refusée : on retombe sur les photos simples
        logger.warning("Cartes produits refusées par WhatsApp : envoi en photos (%s produit(s))", len(items))
    sent = []
    for item in items:
        try:
            await wa_client.send_image_message(to=to, link=item["link"], caption=item["card"])
            sent.append(item)
        except Exception:  # noqa: BLE001 — une photo refusée ne bloque jamais la conversation
            logger.warning("Photo de produit non envoyée (produit %s)", item["product_id"])
    return "images", sent


# Le client parle de prix, ou avance vers l'achat (intérêt, commande, récapitulatif) : les prix restent.
_ASKS_PRICE = re.compile(
    r"(prix|combien|co[uû]te?|tarif|cher|€|euros?|fcfa|xof|budget|promo|r[ée]duction|remise|int[ée]ress|prends|"
    r"command|ach[eè]t|valid|\boui\b|panier|total|payer|paiement)", re.IGNORECASE)
_PRICE_IN_ITEM = re.compile(
    r"\s*(?:[–—:-]|\(|à)?\s*\d[\d\s.,\u202f\u00a0]*\s?(?:€|eur\b|euros?\b|xof\b|fcfa\b|cfa\b|f\b|\$|usd\b)\)?",
    re.IGNORECASE)


def without_unasked_prices(text: str, incoming: str | None) -> str:
    """Lot 76 (test réel du 16/10, 21:43) — « prix seulement sur demande » tenu par le code : si le client n'a pas parlé
    de prix, les prix sont retirés des lignes de liste de Bob (« • Sac à main dame – 220 000 XOF » → « • Sac à main dame »)."""
    if not text or _ASKS_PRICE.search(incoming or ""):
        return text
    lines = []
    for line in text.splitlines():
        if _LIST_ITEM.match(line):
            line = _PRICE_IN_ITEM.sub("", line).rstrip(" ,;–—-")
        lines.append(line)
    return "\n".join(lines)


# --- Lot 73 h — retour réel du 20/10 (MS4 Africa) : « Voici la photo » sans photo, « pas encore de photo » juste après
# l'avoir envoyée, « Souhaitez-vous une photo ? » alors que la carte part dans la même réponse. -------------------------

_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*[^\S\n]*|\n")
OFFERS_PHOTO = re.compile(r"(envoie|envoyer|montre|montrer|partage|partager)\b[^.!?\n]{0,40}\b(photos?|images?|visuels?)"
                          r"|\b(photos?|images?|visuels?)\b[^.!?\n]{0,20}\?", re.IGNORECASE)
ANNOUNCES_PHOTO = re.compile(r"\bvoici\b[^.!?\n]{0,20}\b(photos?|images?|visuels?)\b"
                             r"|\bje vous (envoie|transmets)\b[^.!?\n]{0,20}\b(photos?|images?)\b", re.IGNORECASE)
DENIES_PHOTO = re.compile(r"(?:pas|aucune?|plus)\s+(?:encore\s+)?(?:de\s+|d['’])?(?:photos?|images?|visuels?)"
                          r"|(?:ne\s+(?:peux|pourrai|suis\s+pas\s+en\s+mesure)|impossible|pas\s+pu)[^.!?\n]{0,40}(?:photos?|images?)"
                          r"|\b(enverra|enverrons|transmettra)\b[^.!?\n]{0,40}\b(photos?|images?|visuels?)\b", re.IGNORECASE)


def drop_sentences(text: str, pattern) -> str:
    """Retire les phrases qui correspondent ; garde le reste tel quel (jamais une réponse vide : texte d'origine)."""
    if not text or not pattern.search(text):
        return text
    kept = "".join(part for part in _SENTENCE.findall(text) if part == "\n" or not pattern.search(part))
    # une ligne qui ne garde qu'un émoji ou une ponctuation (« 😊 ») ne veut plus rien dire seule
    kept = "\n".join(line for line in kept.split("\n") if not line.strip() or re.search(r"\w", line))
    kept = re.sub(r"[ \t]+\n", "\n", kept)
    kept = re.sub(r"\n{3,}", "\n\n", kept).strip()
    return kept if re.search(r"\w", kept) else text


def photo_mentions_fixed(text: str, cards_going: bool, photo_known: bool) -> str:
    """Accorde le texte de Bob avec ce qui part vraiment :
    - des cartes partent → pas de « Souhaitez-vous une photo ? » ;
    - rien ne part → pas de « Voici la photo » ;
    - le produit a une photo (montrée ou qui part) → jamais « pas de photo », ni « un conseiller enverra les visuels »."""
    if cards_going:
        text = drop_sentences(text, OFFERS_PHOTO)
    else:
        text = drop_sentences(text, ANNOUNCES_PHOTO)
    if cards_going or photo_known:
        text = drop_sentences(text, DENIES_PHOTO)
    return text
