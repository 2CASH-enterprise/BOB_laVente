"""
Lot 86 — fiche client « vendeur conseil » : ce que le client a DIT (ou fait, par ses commandes), retenu au fil des
échanges pour mieux le servir la fois suivante (tailles, rayon, couleurs, occasion, quartier, adresse, paiement…).

Jamais une supposition de l'IA (pas de sexe deviné : on retient le rayon demandé). Le client en est informé une fois
(lot 86, information au premier échange) et peut tout effacer en écrivant « oubliez-moi ».
Rangé dans customers.detected_preferences (JSON) ; l'adresse de livraison dans customers.address.
"""
# clé → (libellé pour Bob et le commerçant, longueur max)
FIELDS: dict[str, tuple[str, int]] = {
    "need": ("Recherche", 120),
    "section": ("Rayon", 40),
    "clothing_size": ("Taille de vêtement", 20),
    "shoe_size": ("Pointure", 10),
    "colors": ("Couleurs préférées", 80),
    "materials": ("Matières préférées", 80),
    "occasions": ("Occasions d'achat", 120),
    "brand": ("Marque préférée", 60),
    "budget_max": ("Budget maximum mentionné", 20),
    "district": ("Quartier", 80),
    "payment": ("Moyen de paiement préféré", 40),
}
LIST_FIELDS = {"occasions"}  # s'accumulent (mariage, travail…) au lieu de se remplacer
MAX_LIST = 5


def _clean(value, limit: int) -> str | None:
    text = " ".join(str(value or "").split())[:limit]
    return text or None


def merge(preferences: dict | None, tool_input: dict) -> dict:
    """Ajoute ce que le client vient de dire ; ne retire jamais rien de ce qu'il a dit avant."""
    prefs = dict(preferences or {})
    for key, (_, limit) in FIELDS.items():
        if key not in tool_input or tool_input[key] in (None, ""):
            continue
        if key == "budget_max":
            try:
                prefs[key] = float(tool_input[key])
            except (TypeError, ValueError):
                continue
            continue
        value = _clean(tool_input[key], limit)
        if value is None:
            continue
        if key in LIST_FIELDS:
            items = [v for v in (prefs.get(key) or []) if isinstance(v, str)]
            if value.lower() not in [v.lower() for v in items]:
                items.append(value)
            prefs[key] = items[-MAX_LIST:]
        else:
            prefs[key] = value
    return prefs


def lines(preferences: dict | None, address: str | None = None) -> list[tuple[str, str]]:
    """(libellé, valeur) dans l'ordre des FIELDS, puis l'adresse de livraison."""
    prefs = preferences or {}
    out = []
    for key, (label, _) in FIELDS.items():
        value = prefs.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        elif key == "budget_max":
            value = f"{float(value):,.0f}".replace(",", " ")
        out.append((label, str(value)))
    if address:
        out.append(("Adresse de livraison", address))
    return out


# --- Information du client et effacement (« oubliez-moi ») -------------------------------------------------------

import re  # noqa: E402

FORGET = re.compile(r"^\W*(oubliez[- ]moi|oublie[- ]moi|effacez[- ]moi|efface[- ]moi|supprimez mes (donn[ée]es|"
                    r"informations|infos)|supprime mes (donn[ée]es|informations|infos))\W*$", re.IGNORECASE)
FORGOTTEN_REPLY = ("C'est fait : vos préférences (tailles, goûts, adresse) sont effacées. Vos commandes restent "
                   "enregistrées, comme la loi l'exige. Je reste à votre disposition.")
FORGOTTEN_REPLY_TU = ("C'est fait : tes préférences (tailles, goûts, adresse) sont effacées. Tes commandes restent "
                      "enregistrées, comme la loi l'exige. Je reste à ta disposition.")


def is_forget_request(text: str | None) -> bool:
    return bool(FORGET.match(text or ""))


def privacy_line(shop_name: str, tu: bool = False) -> str:
    """Une seule fois, au premier échange : la collecte est naturelle, jamais cachée (loi 2013-450, RGPD)."""
    if tu:
        return (f"Pour mieux te conseiller, {shop_name} garde tes préférences (tailles, goûts, adresse). "
                "Écris « oubliez-moi » à tout moment pour les effacer.")
    return (f"Pour mieux vous conseiller, {shop_name} garde vos préférences (tailles, goûts, adresse). "
            "Écrivez « oubliez-moi » à tout moment pour les effacer.")


async def forget(db, customer) -> None:
    """Efface ce que Bob a retenu du client (préférences, adresse, produits consultés) ; les commandes restent."""
    from sqlalchemy import delete

    from app.models.customer_product_view import CustomerProductView

    customer.detected_preferences = {}
    customer.address = None
    await db.execute(delete(CustomerProductView).where(CustomerProductView.tenant_id == customer.tenant_id,
                                                       CustomerProductView.customer_id == customer.id))
    from app.models.cart import Cart  # lot 95 : le panier en cours (adresse comprise) part aussi

    await db.execute(delete(Cart).where(Cart.tenant_id == customer.tenant_id, Cart.customer_id == customer.id))
