"""
Type d'activité de la boutique (lot 24) : un seul Bob, un comportement par secteur.

- ONLINE_STORE (défaut) : Bob vend et conclut sur WhatsApp (commande, lien de paiement,
  négociation). Comportement d'avant ce lot, strictement inchangé.
- CAR_DEALERSHIP : on ne vend pas une voiture sur WhatsApp. Bob renseigne, qualifie et
  obtient un rendez-vous (essai, visite, estimation de reprise) ; le vendeur conclut.

Les outils de vente sont RETIRÉS à l'IA en mode concession (verrou dans le code, pas une
simple consigne) : elle ne peut ni les voir ni les exécuter.
"""
ONLINE_STORE = "ONLINE_STORE"
CAR_DEALERSHIP = "CAR_DEALERSHIP"

BUSINESS_TYPES = {
    ONLINE_STORE: {
        "label": "Boutique en ligne",
        "description": "Bob répond, conseille et prend les commandes sur WhatsApp.",
    },
    CAR_DEALERSHIP: {
        "label": "Concession automobile",
        "description": "Bob renseigne sur vos véhicules, qualifie le client et obtient un rendez-vous (essai, visite, reprise).",
    },
}

# Outils de vente directe : jamais en concession.
SALES_TOOLS = frozenset({
    "create_order",
    "share_payment_link",
    "negotiate_price",
    "suggest_complementary_products",
    "get_frequently_bought_together",
})
# Outils propres à la concession : jamais en boutique en ligne.
DEALERSHIP_TOOLS = frozenset({"request_appointment"})


def normalize(value: str | None) -> str:
    return value if value in BUSINESS_TYPES else ONLINE_STORE


def is_dealership(tenant) -> bool:
    return normalize(getattr(tenant, "business_type", None)) == CAR_DEALERSHIP


def tool_allowed(business_type: str | None, tool_name: str) -> bool:
    if normalize(business_type) == CAR_DEALERSHIP:
        return tool_name not in SALES_TOOLS
    return tool_name not in DEALERSHIP_TOOLS


def tools_for(business_type: str | None, tool_definitions: list[dict]) -> list[dict]:
    return [t for t in tool_definitions if tool_allowed(business_type, t["name"])]
