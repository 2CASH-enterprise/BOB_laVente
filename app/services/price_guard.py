"""
Lot 93 — garde-fou des prix (constat de Thérèse du 31/10 : « assez souvent les prix ne sont pas conformes à ceux
affichés sur les catalogues »).

Une consigne (« ne jamais inventer un prix ») ne suffit pas : c'est le CODE qui relit chaque montant écrit par l'IA
avant l'envoi (même principe que promise_guard, lot 16, et finance_guard, lot 24).

Un montant est accepté s'il existe vraiment pour cette boutique et cette conversation :
- prix d'un produit actif, prix barré, prix d'une variante, et leurs multiples (2 robes = 2 × le prix) ;
- tout montant lu par les outils de Bob pendant ce tour (prix, total d'une commande, prix négocié…) ;
- tout montant des informations de la boutique données à Bob (frais de livraison…) ;
- tout montant écrit par le client lui-même (« je propose 12 000 ») ;
- les totaux des commandes de ce client, et la somme de deux montants connus (total + livraison).
Sinon : une correction est demandée à l'IA (un seul essai) ; si le montant reste faux, la phrase est retirée.
"""
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

MAX_MULTIPLE = 20
_CURRENCY = (r"(?:€|eur\b|euros?\b|fcfa\b|f\s?cfa\b|cfa\b|xof\b|xaf\b|francs?\b|f\b|\$|usd\b|dollars?\b|gnf\b|"
             r"mru\b|gmd\b|sle\b|lrd\b|ghs\b|cedis?\b|ngn\b|nairas?\b|cdf\b|mad\b|dirhams?\b|dzd\b|tnd\b|chf\b|cad\b)")
_NUMBER = r"\d{1,3}(?:[   .,]\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?"
# Montant suivi d'une devise (« 15 000 FCFA », « 15.000F », « 49,90 € ») ou précédé d'une devise (« $ 25 »).
_AMOUNT_AFTER = re.compile(rf"(?<![\w.,])({_NUMBER})\s?(k\b|mille\b)?\s?{_CURRENCY}", re.IGNORECASE)
_AMOUNT_BEFORE = re.compile(rf"(?:€|\$)\s?({_NUMBER})", re.IGNORECASE)
# Montant annoncé comme un prix sans devise (« Prix : 15000 », « il coûte 15 000 », « total de 30 000 »).
# Un nombre suivi d'un mot (« total de vos 2 articles ») est une quantité, pas un montant.
_AMOUNT_WORDED = re.compile(rf"\b(?:prix|co[uû]te(?:nt)?|tarif|total|montant|vendu[es]? à|à seulement)\b[^\d\n]{{0,12}}"
                            rf"({_NUMBER})(?![\d.,]?\d|\s?\d|\s?[^\W\d_]|\s?%)", re.IGNORECASE)
_ANY_NUMBER = re.compile(_NUMBER)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*[^\S\n]*|\n")


def to_amount(raw: str, thousands: str | None = None) -> Decimal | None:
    """« 15 000 » / « 15.000 » / « 15,000 » / « 49,90 » / « 15 » + « k » → Decimal."""
    text = raw.strip().replace(" ", " ").replace(" ", " ")
    if re.fullmatch(r"\d{1,3}(?:[ .,]\d{3})+", text):
        text = re.sub(r"[ .,]", "", text)
    elif re.fullmatch(r"\d{1,3}(?:[ .]\d{3})+,\d{1,2}", text):
        text = re.sub(r"[ .]", "", text).replace(",", ".")
    else:
        text = text.replace(" ", "").replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if thousands:
        value *= 1000
    return value.quantize(Decimal("0.01"))


def amounts_in(text: str | None) -> list[tuple[Decimal, str]]:
    """Les montants d'argent que le texte annonce, avec le passage qui les contient."""
    found = []
    for pattern in (_AMOUNT_AFTER, _AMOUNT_BEFORE, _AMOUNT_WORDED):
        for match in pattern.finditer(text or ""):
            thousands = match.group(2) if pattern is _AMOUNT_AFTER else None
            value = to_amount(match.group(1), thousands)
            if value is not None and value > 0:
                found.append((value, match.group(0)))
    return found


def numbers_in(text: str | None) -> set[Decimal]:
    """Tous les nombres d'un texte (outils, informations de la boutique, messages du client)."""
    out = set()
    for match in _ANY_NUMBER.finditer(text or ""):
        value = to_amount(match.group(0))
        if value is not None and value > 0:
            out.add(value)
    return out


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01")) if value not in (None, "") else None
    except InvalidOperation:
        return None


def catalog_prices(products) -> set[Decimal]:
    prices = set()
    for product in products:
        candidates = [product.price, (product.sheet or {}).get("compare_at_price")]
        candidates += [v.get("price") for v in (product.variants or []) if isinstance(v, dict)]
        for raw in candidates:
            value = _decimal(raw)
            if value is not None and value > 0:
                prices.add(value)
    return prices


async def known_amounts(db, tenant_id, customer_id=None, extra_texts: list[str] | None = None) -> set[Decimal]:
    from app.models.order import Order
    from app.models.product import Product

    products = (await db.execute(select(Product).where(Product.tenant_id == tenant_id, Product.active.is_(True))
                                 .limit(5000))).scalars().all()
    unit = catalog_prices(products)
    allowed = {p * q for p in unit for q in range(1, MAX_MULTIPLE + 1)}
    context = set()
    for text in extra_texts or []:
        context |= numbers_in(text)
    if customer_id is not None:
        totals = (await db.execute(select(Order.total_amount).where(Order.tenant_id == tenant_id,
                                                                    Order.customer_id == customer_id)
                                   .order_by(Order.created_at.desc()).limit(20))).scalars().all()
        context |= {v for v in (_decimal(t) for t in totals) if v is not None}
    allowed |= context
    small = sorted(context | unit)[:200]  # somme de deux montants connus (total + frais de livraison…)
    allowed |= {a + b for i, a in enumerate(small) for b in small[i:]}
    return allowed


def wrong_amounts(text: str, allowed: set[Decimal]) -> list[str]:
    return [passage for value, passage in amounts_in(text) if value not in allowed]


def correction(wrong: list[str]) -> str:
    listed = ", ".join(f"« {w.strip()} »" for w in wrong[:3])
    return ("\n\nCONSIGNE (correction) : ta réponse annonce " + listed + ", un montant qui ne correspond à aucun prix "
            "du catalogue de la boutique. Ne donne JAMAIS un prix de mémoire : appelle get_product_price ou "
            "search_products, puis recopie le prix EXACT renvoyé par l'outil (ou ne donne pas de prix).")


def without_wrong_amounts(text: str, allowed: set[Decimal]) -> str:
    """Retire les phrases qui annoncent un montant inconnu ; renvoie '' s'il ne reste rien d'utile."""
    kept = "".join(part for part in _SENTENCE.findall(text or "")
                   if part == "\n" or not wrong_amounts(part, allowed))
    kept = "\n".join(line for line in kept.split("\n") if not line.strip() or re.search(r"\w", line))
    kept = re.sub(r"\n{3,}", "\n\n", kept).strip()
    return kept if re.search(r"\w{2}", kept) else ""


FALLBACK_WITH_CARDS = "Voici le prix exact :"
FALLBACK_NO_PRODUCT = "Je préfère vous donner le prix exact : quel produit vous intéresse ?"
