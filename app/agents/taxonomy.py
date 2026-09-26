"""
Taxonomie des intentions et objections (phase 1 de l'apprentissage, version 1).

SOURCE UNIQUE : le classificateur, la validation de ses réponses et le dashboard lisent
tous cette liste. Volontairement courte : une liste réduite est classée plus fiablement.
Les codes sont stables (stockés en base) ; seuls les libellés peuvent évoluer.
"""

# v1.1 : exemples ajoutés aux consignes, « Hésitation » précisée, « Salutation » élargie à la
# politesse. Les CODES sont inchangés : les étiquettes v1 et v1.1 restent comparables.
# v1.2 (lot 16) : nouvelle intention CONDITIONS_VENTE. Constat du 26/09 : « Vous acceptez les
# retours ? » était classé REMBOURSEMENT/RÉCLAMATION (transfert inutile). Les autres codes ne
# changent pas ; seules ces questions quittent REMBOURSEMENT/RECLAMATION à partir de v1.2.
TAXONOMY_VERSION = "v1.2"

INTENTS: dict[str, tuple[str, str]] = {
    # code: (libellé affiché, définition donnée au classificateur)
    "SALUTATION": ("Salutation ou politesse", "bonjour, merci, d'accord, ok, au revoir, sans autre demande"),
    "RECHERCHE_PRODUIT": ("Recherche de produit", "cherche un produit, un modèle, une catégorie"),
    "DEMANDE_PRIX": ("Demande de prix", "demande combien coûte un produit"),
    "DISPONIBILITE": ("Disponibilité", "demande si un produit, une taille ou une couleur est disponible"),
    "DEMANDE_REMISE": ("Demande de remise", "demande une réduction, un meilleur prix, propose un prix plus bas"),
    "LIVRAISON": ("Livraison", "question sur la livraison : zone, délai, frais, modalités"),
    "PAIEMENT": ("Paiement", "question sur les moyens ou modalités de paiement"),
    "INTENTION_ACHAT": ("Intention d'achat", "veut acheter, commander, réserver, « je prends »"),
    "SUIVI_COMMANDE": ("Suivi de commande", "demande où en est une commande déjà passée"),
    "CONDITIONS_VENTE": (
        "Question sur les conditions de vente",
        "demande quelles sont les conditions de retour, d'échange ou de garantie, sans problème réel "
        "avec une commande (« vous acceptez les retours ? », « je peux échanger si la taille ne va pas ? »)",
    ),
    "RECLAMATION": ("Réclamation", "se plaint d'un problème réel : produit abîmé, erreur, retard, mécontentement"),
    "REMBOURSEMENT": (
        "Remboursement",
        "demande à être remboursé ou à retourner un achat qu'il a fait (pas une simple question sur la politique de retour)",
    ),
    "DEMANDE_HUMAIN": ("Demande d'un humain", "demande explicitement à parler à une personne, un conseiller, le responsable"),
    "AUTRE": ("Autre", "rien de ce qui précède"),
}

OBJECTIONS: dict[str, tuple[str, str]] = {
    "PRIX_TROP_ELEVE": ("Prix trop élevé", "trouve le produit trop cher, n'a pas le budget, compare à moins cher"),
    "FRAIS_LIVRAISON": ("Frais de livraison", "trouve la livraison trop chère"),
    "CONFIANCE": ("Confiance", "doute de la fiabilité : arnaque, paiement avant livraison, produit réel"),
    "QUALITE": ("Qualité", "doute de la qualité, de l'authenticité ou de la durabilité"),
    "DELAI": ("Délai", "trouve le délai de livraison ou de disponibilité trop long"),
    "RUPTURE_STOCK": ("Rupture de stock", "le produit voulu n'est pas disponible"),
    "HESITATION": (
        "Hésitation",
        "reporte sa décision sans donner de raison : « je vais réfléchir », « je reviens plus tard », "
        "« je dois demander à mon mari » — c'est une objection même si aucune raison n'est donnée",
    ),
}


def intent_label(code: str) -> str:
    return INTENTS.get(code, (code, ""))[0]


def objection_label(code: str) -> str:
    return OBJECTIONS.get(code, (code, ""))[0]
