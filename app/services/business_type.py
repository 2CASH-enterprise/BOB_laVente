"""
Type d'activité de la boutique (lot 24) : un seul Bob, un comportement par secteur.

- ONLINE_STORE (défaut) : Bob vend et conclut sur WhatsApp (commande, lien de paiement,
  négociation). Comportement d'avant ce lot, strictement inchangé.
- CAR_DEALERSHIP : on ne vend pas une voiture sur WhatsApp. Bob renseigne, qualifie et
  obtient un rendez-vous (essai, visite, estimation de reprise) ; le vendeur conclut.
- INSURANCE_BROKER (lot 53) : courtier ou agent d'assurance. Bob renseigne sur les produits
  d'assurance, sans JAMAIS donner de prix ni de garantie chiffrée, qualifie le besoin branche par
  branche, transmet une demande de cotation au cabinet et propose un appel ou un rendez-vous.

- REAL_ESTATE (lot 79) : agence immobilière ou mandataire. Bob trie les 4 publics (acheteur, locataire,
  vendeur, propriétaire bailleur), présente les biens en cartes, qualifie le projet et obtient une visite,
  une estimation (le mandat) ou un rendez-vous ; il ne promet jamais un prix de vente, cite toujours les
  honoraires avec un prix et ne demande au locataire que les pièces autorisées par la loi.

Concession, courtier et agence immobilière forment la famille des « secteurs à rendez-vous » (aucune commande,
rendez-vous, commerciaux) ; ce qui est propre aux véhicules reste réservé à la concession.

Les outils de vente sont RETIRÉS à l'IA en mode concession (verrou dans le code, pas une
simple consigne) : elle ne peut ni les voir ni les exécuter.
"""
ONLINE_STORE = "ONLINE_STORE"
CAR_DEALERSHIP = "CAR_DEALERSHIP"
INSURANCE_BROKER = "INSURANCE_BROKER"
REAL_ESTATE = "REAL_ESTATE"

BUSINESS_TYPES = {
    ONLINE_STORE: {
        "label": "Commerce",
        "hint": "En ligne et physique",  # lot 53 : affiché en gris sous le nom
        "description": "Bob répond, conseille et prend les commandes sur WhatsApp.",
    },
    CAR_DEALERSHIP: {
        "label": "Concession auto / moto",  # lot 78 : les motos relèvent de la concession
        "hint": None,
        "description": "Bob renseigne sur vos véhicules, qualifie le client et obtient un rendez-vous (essai, visite, reprise).",
    },
    INSURANCE_BROKER: {
        "label": "Courtier / agent d'assurance",
        "hint": None,
        "description": "Bob renseigne sur vos assurances, prépare la demande de cotation et propose un appel ou un rendez-vous. Jamais de prix donné par Bob.",
    },
    REAL_ESTATE: {
        "label": "Agence immobilière",
        "hint": "Vente, location, gestion",
        "description": "Bob présente vos biens, qualifie acheteurs, locataires et propriétaires, et obtient visites et estimations.",
    },
}
APPOINTMENT_SECTORS = frozenset({CAR_DEALERSHIP, INSURANCE_BROKER, REAL_ESTATE})

# Lot 53 — types de rendez-vous proposés par Bob, par secteur.
APPOINTMENT_KINDS_BY_SECTOR = {
    CAR_DEALERSHIP: ("ESSAI", "VISITE", "ESTIMATION_REPRISE"),
    INSURANCE_BROKER: ("CABINET", "APPEL"),
    REAL_ESTATE: ("VISITE", "ESTIMATION", "AGENCE", "APPEL"),  # lot 79
}

# Outils de vente directe : jamais en concession.
SALES_TOOLS = frozenset({
    "create_order",
    "share_payment_link",
    "negotiate_price",
    "suggest_complementary_products",
    "get_frequently_bought_together",
})
# Outils des secteurs à rendez-vous : jamais en commerce.
DEALERSHIP_TOOLS = frozenset({
    "request_appointment", "get_available_slots",
    "get_my_appointments", "reschedule_my_appointment", "cancel_my_appointment",  # lot 35
    "update_prospect_profile",  # lot 43 : fiche prospect
})
# Lot 53 — propres à la concession (véhicules) ou au courtier (demande de cotation).
VEHICLE_ONLY_TOOLS = frozenset({"update_prospect_profile"})
INSURANCE_ONLY_TOOLS = frozenset({"update_insurance_request"})
# Lot 79 — propre à l'agence immobilière : la fiche projet (achat, location, vente, gestion).
REAL_ESTATE_ONLY_TOOLS = frozenset({"update_property_project"})
# Lot 53 — un courtier ne donne jamais de prix ni de stock : ces outils lui sont retirés.
INSURANCE_FORBIDDEN_TOOLS = SALES_TOOLS | {"check_stock", "get_product_price", "check_order_status", "send_product_images"}


def normalize(value: str | None) -> str:
    return value if value in BUSINESS_TYPES else ONLINE_STORE


def is_dealership(tenant) -> bool:
    return normalize(getattr(tenant, "business_type", None)) == CAR_DEALERSHIP


def is_store(tenant) -> bool:
    """Lot 78 — secteur Commerce (fiches produits par catégorie)."""
    return normalize(getattr(tenant, "business_type", None)) == ONLINE_STORE


def is_insurance(tenant) -> bool:
    return normalize(getattr(tenant, "business_type", None)) == INSURANCE_BROKER


def is_real_estate(tenant) -> bool:
    return normalize(getattr(tenant, "business_type", None)) == REAL_ESTATE


def is_appointment_sector(tenant) -> bool:
    """Concession, courtier ou agence immobilière : rendez-vous, commerciaux, jamais de commande."""
    return normalize(getattr(tenant, "business_type", None)) in APPOINTMENT_SECTORS


def is_online_store(tenant) -> bool:
    return normalize(getattr(tenant, "business_type", None)) == ONLINE_STORE


def appointment_kinds(business_type: str | None) -> tuple[str, ...]:
    return APPOINTMENT_KINDS_BY_SECTOR.get(normalize(business_type), ())


def tool_allowed(business_type: str | None, tool_name: str) -> bool:
    sector = normalize(business_type)
    if sector == CAR_DEALERSHIP:
        return tool_name not in SALES_TOOLS | INSURANCE_ONLY_TOOLS | REAL_ESTATE_ONLY_TOOLS
    if sector == INSURANCE_BROKER:
        return tool_name not in INSURANCE_FORBIDDEN_TOOLS | VEHICLE_ONLY_TOOLS | REAL_ESTATE_ONLY_TOOLS
    if sector == REAL_ESTATE:
        return tool_name not in SALES_TOOLS | VEHICLE_ONLY_TOOLS | INSURANCE_ONLY_TOOLS
    return tool_name not in DEALERSHIP_TOOLS | INSURANCE_ONLY_TOOLS | REAL_ESTATE_ONLY_TOOLS


# Lot 26 — en concession, la recherche accepte les critères d'un véhicule.
VEHICLE_SEARCH_PROPERTIES = {
    "fuel": {"type": "string", "enum": ["ESSENCE", "DIESEL", "HYBRIDE", "ELECTRIQUE", "GPL"],
             "description": "Carburant souhaité, seulement si le client l'a précisé"},
    "gearbox": {"type": "string", "enum": ["MANUELLE", "AUTOMATIQUE"],
                "description": "Boîte souhaitée, seulement si le client l'a précisée"},
    "body_type": {"type": "string", "enum": ["SUV", "BERLINE", "CITADINE", "BREAK", "MONOSPACE", "COUPE", "CABRIOLET", "PICK_UP", "UTILITAIRE"],
                  "description": "Carrosserie souhaitée (SUV, berline, citadine…), seulement si le client l'a précisée"},
    "min_year": {"type": "integer", "description": "Année minimale, seulement si le client l'a précisée"},
    "max_mileage_km": {"type": "integer", "description": "Kilométrage maximal, seulement si le client l'a précisé"},
}


def tools_for(business_type: str | None, tool_definitions: list[dict]) -> list[dict]:
    import copy

    tools = [t for t in tool_definitions if tool_allowed(business_type, t["name"])]
    if normalize(business_type) == INSURANCE_BROKER:
        return [_for_insurance(t) for t in tools]
    if normalize(business_type) == REAL_ESTATE:
        return [_for_real_estate(t) for t in tools]
    if normalize(business_type) != CAR_DEALERSHIP:
        return tools
    adapted = []
    for tool in tools:
        if tool["name"] == "search_products":
            tool = copy.deepcopy(tool)
            tool["description"] = (
                "Recherche des véhicules dans le stock de la concession, par nom (marque, modèle) et, si le "
                "client les a donnés, par carrosserie (SUV, berline…), carburant, boîte, année minimale ou "
                "kilométrage maximal. Laisse query vide pour une recherche par critères seulement. Retourne "
                "uniquement des véhicules réels avec leur prix, leur disponibilité et leurs caractéristiques. "
                "N'invente jamais un véhicule ni une caractéristique absente du résultat."
            )
            tool["input_schema"]["properties"].update(copy.deepcopy(VEHICLE_SEARCH_PROPERTIES))
            tool["input_schema"]["required"] = []
        adapted.append(tool)
    return adapted


def _for_insurance(tool: dict) -> dict:
    """Lot 53 — mêmes outils de rendez-vous, adaptés au cabinet (jamais de véhicule ni de prix)."""
    import copy

    name = tool["name"]
    if name not in ("search_products", "recommend_products", "request_appointment"):
        return tool
    tool = copy.deepcopy(tool)
    if name == "search_products":
        tool["description"] = (
            "Recherche les produits d'assurance proposés par le cabinet (auto, santé, habitation…), par nom ou "
            "par mot-clé. Retourne leur description, écrite par le cabinet. Aucun prix : le cabinet fait une "
            "proposition personnalisée. N'invente jamais une garantie absente de la description."
        )
        for key in ("min_price", "max_price"):
            tool["input_schema"]["properties"].pop(key, None)
    elif name == "recommend_products":
        tool["description"] = ("Propose les produits d'assurance du cabinet qui correspondent au besoin exprimé. "
                               "Aucun prix : jamais de montant dans ta réponse.")
        tool["input_schema"]["properties"].pop("budget", None)
    else:
        props = tool["input_schema"]["properties"]
        props["kind"] = {"type": "string", "enum": list(APPOINTMENT_KINDS_BY_SECTOR[INSURANCE_BROKER]),
                         "description": "CABINET = rendez-vous au cabinet ; APPEL = un conseiller appelle le client"}
        props["product_id"] = {"type": "string", "description": "UUID du produit d'assurance concerné, s'il est connu"}
        props["vehicle"] = {"type": "string", "description": "Assurance concernée, avec les mots du client (ex. « assurance auto »)"}
        for key in ("budget", "trade_in", "financing_interest"):
            props.pop(key, None)
        tool["description"] = (
            "Enregistre un rendez-vous avec le cabinet : au cabinet (CABINET) ou un appel d'un conseiller (APPEL). "
            "Avec un créneau proposé par get_available_slots (slot), il est confirmé tout de suite ; sans créneau, "
            "la demande est transmise au cabinet qui confirmera."
        )
    return tool


# Lot 79 — agence immobilière : critères d'un bien dans la recherche.
PROPERTY_SEARCH_PROPERTIES = {
    "transaction": {"type": "string", "enum": ["VENTE", "LOCATION"],
                    "description": "Achat (VENTE) ou location (LOCATION), seulement si le client l'a dit"},
    "property_type": {"type": "string", "enum": ["APPARTEMENT", "MAISON", "TERRAIN", "LOCAL", "PARKING", "AUTRE"],
                      "description": "Type de bien, seulement si le client l'a précisé (studio, T2… = APPARTEMENT)"},
    "city": {"type": "string", "description": "Ville ou quartier souhaité, seulement si le client l'a donné"},
    "min_rooms": {"type": "integer", "description": "Nombre de pièces minimal (T3 = 3 pièces), seulement si précisé"},
    "min_surface": {"type": "integer", "description": "Surface minimale en m², seulement si précisée"},
}


def _for_real_estate(tool: dict) -> dict:
    """Lot 79 — mêmes outils de rendez-vous, adaptés à l'agence : des biens, jamais de véhicule ni de reprise."""
    import copy

    name = tool["name"]
    if name not in ("search_products", "recommend_products", "request_appointment", "send_product_images"):
        return tool
    tool = copy.deepcopy(tool)
    if name == "search_products":
        tool["description"] = (
            "Recherche des biens de l'agence (à vendre ou à louer), par mots du client et, s'il les a donnés, par "
            "achat ou location, type de bien, ville ou quartier, nombre de pièces minimal, surface minimale et budget "
            "maximal (max_price : prix de vente, ou loyer mensuel charges comprises). Laisse query vide pour une "
            "recherche par critères seulement. Retourne uniquement des biens réels, avec leur fiche. N'invente jamais "
            "un bien ni une caractéristique absente du résultat."
        )
        tool["input_schema"]["properties"].update(copy.deepcopy(PROPERTY_SEARCH_PROPERTIES))
        tool["input_schema"]["properties"].pop("min_price", None)
        tool["input_schema"]["required"] = []
    elif name == "recommend_products":
        tool["description"] = ("Propose les biens de l'agence qui correspondent au besoin exprimé (budget : prix de "
                               "vente, ou loyer mensuel charges comprises).")
    elif name == "send_product_images":
        tool["description"] = (
            "Montre au client, sur WhatsApp, un à trois biens de l'agence en CARTES : la vraie photo, le type, la "
            "surface, la ville et un bouton « Ça m'intéresse ». À utiliser dès que tu présentes des biens (« vous avez "
            "des T3 à louer ? ») ou quand le client demande une photo. Les cartes partent juste après ta réponse : ta "
            "réponse est alors une courte phrase d'introduction, sans lister les biens ni leurs prix. Si un bien n'a "
            "pas de photo, l'outil te le dit (cite-le alors simplement)."
        )
    else:
        props = tool["input_schema"]["properties"]
        props["kind"] = {"type": "string", "enum": list(APPOINTMENT_KINDS_BY_SECTOR[REAL_ESTATE]),
                         "description": "VISITE = visite d'un bien ; ESTIMATION = estimation du bien du client (vente ou "
                                        "mise en location) ; AGENCE = rendez-vous à l'agence ; APPEL = un conseiller "
                                        "appelle le client"}
        props["product_id"] = {"type": "string", "description": "UUID du bien à visiter, s'il est au catalogue"}
        props["vehicle"] = {"type": "string", "description": "Bien concerné avec les mots du client (ex. « T3 rue "
                                                             "Victor-Hugo », « ma maison à Villeurbanne »)"}
        props["need"] = {"type": "string", "description": "Projet du client, avec ses mots, s'il l'a dit"}
        for key in ("trade_in", "financing_interest"):
            props.pop(key, None)
        tool["description"] = (
            "Enregistre un rendez-vous avec l'agence : visite d'un bien (VISITE), estimation du bien du client "
            "(ESTIMATION), rendez-vous à l'agence (AGENCE) ou appel d'un conseiller (APPEL). Avec un créneau proposé "
            "par get_available_slots (slot), il est confirmé tout de suite et le client reçoit la confirmation ; sans "
            "créneau, la demande est transmise à l'agence qui confirmera — tu ne confirmes alors JAMAIS toi-même une "
            "date ou une heure."
        )
    return tool


# --- Lot 32 : fonctions réservées à un type d'activité (verrou côté serveur) ---------------

NOT_AVAILABLE = "Cette fonction n'est pas disponible pour votre type d'activité."


def require_business(*allowed: str):
    """
    Dépendance FastAPI : refuse (403) une route réservée à un autre type d'activité. Masquer un
    bouton ne suffit pas : l'appel direct à l'API doit aussi être refusé.
    """
    from fastapi import Depends, HTTPException

    from app.core.database import get_db
    from app.core.security import get_current_user

    async def _check(current_user=Depends(get_current_user), db=Depends(get_db)) -> None:
        from app.models.tenant import Tenant

        tenant = await db.get(Tenant, current_user.tenant_id)
        if tenant is None or normalize(tenant.business_type) not in allowed:
            raise HTTPException(status_code=403, detail=NOT_AVAILABLE)

    return _check


def only_online_store():
    return require_business(ONLINE_STORE)


def only_dealership():
    return require_business(CAR_DEALERSHIP)


def only_appointment_sectors():
    """Lot 53 / 79 — rendez-vous et commerciaux : concession, courtier et agence immobilière."""
    return require_business(*APPOINTMENT_SECTORS)


def only_insurance():
    return require_business(INSURANCE_BROKER)


def only_real_estate():
    return require_business(REAL_ESTATE)
