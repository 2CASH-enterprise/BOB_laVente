"""
Bibliothèque de stratégies de réponse aux objections (phase 2, lot 15).

SOURCE UNIQUE : consignes données à Bob, écran de réglages et statistiques lisent cette liste.
Chaque stratégie est une CONSIGNE pour le message en cours, jamais une permission d'inventer :
prix, stock, délais et conditions viennent toujours des outils et de la base de connaissances.

Sélection (option A validée) : Bob alterne AU HASARD entre les stratégies activées d'une
objection. C'est ce qui permet de comparer honnêtement les stratégies (toutes sont honnêtes,
alterner ne fait prendre aucun risque au client) et prépare l'apprentissage de la phase 3.
"""
import random
from dataclasses import dataclass

STRATEGY_LIBRARY_VERSION = "v1"
MIN_TERMINATED_FOR_RATE = 20  # en dessous, « pas encore assez de données » plutôt qu'un pourcentage trompeur

GUARDRAILS = (
    "Garde-fous : n'invente jamais de prix, de stock, de délai, de promotion ni d'avis client ; "
    "pas de fausse urgence ni de fausse rareté ; n'insiste pas si le client refuse clairement."
)


# Conditions commerciales : catégories de la base de connaissances qu'une stratégie peut citer.
CONDITION_CATEGORIES = frozenset({"PAIEMENT", "LIVRAISON", "RETOUR", "GARANTIE", "CONDITIONS"})

# Consigne de repli quand l'objection porte sur des conditions que la boutique n'a pas renseignées
# (cas réel du 26/09 : base vide, Bob a inventé « retours sous 14 jours »).
NO_KNOWLEDGE_INSTRUCTION = (
    "Le client pose une question sur des conditions que la boutique n'a pas renseignées. N'affirme "
    "AUCUNE condition (paiement, livraison, retours, remboursement, garantie, délai) : dis honnêtement "
    "que tu vas vérifier ce point auprès de la boutique, et demande au client ce qui l'inquiète."
)


@dataclass(frozen=True)
class Strategy:
    code: str
    objection: str
    label: str
    description: str  # pour le commerçant
    instruction: str  # pour Bob
    # Verrou (code, pas consigne) : la stratégie n'est tirée que si AU MOINS UNE de ces catégories
    # est renseignée dans la base de connaissances. Vide = aucune information requise.
    requires: frozenset = frozenset()


STRATEGIES: list[Strategy] = [
    Strategy("PRIX_VALEUR", "PRIX_TROP_ELEVE", "Valeur",
             "Bob explique ce qui justifie le prix, avec les vraies caractéristiques du produit.",
             "Le client trouve le prix élevé. Explique ce qui justifie ce prix en t'appuyant uniquement sur la "
             "description du produit renvoyée par search_products. Si elle est vide, n'invente aucune "
             "caractéristique : mets plutôt en avant ce que tu sais réellement (disponibilité, conditions de la "
             "base de connaissances)."),
    Strategy("PRIX_ALTERNATIVE", "PRIX_TROP_ELEVE", "Alternative",
             "Bob propose un produit moins cher, s'il en existe un dans le catalogue.",
             "Le client trouve le prix élevé. Cherche un produit comparable moins cher (search_products ou "
             "recommend_products) et propose-le s'il existe ; sinon, dis-le simplement."),
    Strategy("PRIX_BUDGET", "PRIX_TROP_ELEVE", "Budget",
             "Bob demande au client quel budget il envisage, pour lui proposer ce qui convient.",
             "Le client trouve le prix élevé. Demande-lui poliment quel budget il envisage, pour pouvoir lui "
             "proposer ce qui lui convient le mieux."),
    Strategy("HESITATION_CLARIFIER", "HESITATION", "Clarifier",
             "Bob cherche à comprendre ce qui fait hésiter : le prix, le modèle ou la livraison ?",
             "Le client hésite. Demande-lui, avec une seule question simple, ce qui le fait hésiter : plutôt le "
             "prix, le modèle ou la livraison ? Propose ensuite ton aide sur ce point."),
    Strategy("HESITATION_RESPECTER", "HESITATION", "Respecter",
             "Bob prend acte, résume le produit en une phrase et reste disponible, sans insister.",
             "Le client hésite. Respecte sa décision : résume en une phrase le produit qui l'intéressait et "
             "indique que tu restes disponible. N'insiste pas et ne pose pas de question."),
    Strategy("CONFIANCE_FAITS", "CONFIANCE", "Rassurer par les faits",
             "Bob rappelle les vraies conditions (paiement, livraison, retours) de la base de connaissances.",
             "Le client doute de la fiabilité. Rassure-le en rappelant uniquement les conditions réelles de la "
             "boutique (paiement, livraison, retours) telles qu'elles figurent dans la base de connaissances. "
             "Si une information n'y figure pas, ne l'affirme pas.",
             requires=CONDITION_CATEGORIES),
    Strategy("CONFIANCE_COMPRENDRE", "CONFIANCE", "Comprendre",
             "Bob demande ce qui inquiète précisément le client, pour y répondre.",
             "Le client doute de la fiabilité. Demande-lui ce qui l'inquiète précisément (paiement, livraison, "
             "produit), pour pouvoir lui répondre avec des faits."),
    Strategy("LIVRAISON_EXPLIQUER", "FRAIS_LIVRAISON", "Expliquer",
             "Bob détaille ce que couvre la livraison et les options réelles.",
             "Le client trouve la livraison chère. Explique ce que couvre la livraison et les options qui existent "
             "réellement, d'après la base de connaissances.",
             requires=frozenset({"LIVRAISON"})),
    Strategy("LIVRAISON_ALTERNATIVE", "FRAIS_LIVRAISON", "Alternative",
             "Bob propose un retrait ou une option moins chère, si elle existe.",
             "Le client trouve la livraison chère. Propose une option moins chère (retrait, autre mode) uniquement "
             "si elle figure dans la base de connaissances ; sinon, dis-le simplement.",
             requires=frozenset({"LIVRAISON"})),
    Strategy("QUALITE_DETAILS", "QUALITE", "Détails",
             "Bob donne les caractéristiques réelles du produit (matière, marque, garantie).",
             "Le client doute de la qualité. Donne les caractéristiques réelles du produit (matière, marque, "
             "garantie) telles qu'elles figurent dans sa description (search_products) ou dans la base de "
             "connaissances, sans rien ajouter. Si elles n'y figurent pas, dis honnêtement que tu n'as pas "
             "ce détail et propose de le demander à la boutique."),
    Strategy("DELAI_ALTERNATIVE", "DELAI", "Alternative disponible",
             "Bob propose un produit équivalent disponible plus vite, s'il en existe un.",
             "Le client trouve le délai trop long. Propose un produit équivalent disponible plus rapidement s'il en "
             "existe un (vérifie son stock avec check_stock) ; sinon, indique honnêtement le délai tel qu'il "
             "figure dans la base de connaissances, sans en inventer."),
    Strategy("RUPTURE_SIMILAIRE", "RUPTURE_STOCK", "Produit similaire",
             "Bob propose le produit disponible le plus proche.",
             "Le produit voulu n'est pas disponible. Propose le produit disponible le plus proche "
             "(recommend_products ou search_products), en vérifiant son stock."),
]

STRATEGIES_BY_CODE = {s.code: s for s in STRATEGIES}

# Une seule objection traitée par message : la plus bloquante d'abord.
OBJECTION_PRIORITY = ["CONFIANCE", "RUPTURE_STOCK", "PRIX_TROP_ELEVE", "FRAIS_LIVRAISON", "DELAI", "QUALITE", "HESITATION"]

_rng = random.Random()


def strategies_for(objection: str) -> list[Strategy]:
    return [s for s in STRATEGIES if s.objection == objection]


def is_available(strategy: Strategy, knowledge_categories: set[str]) -> bool:
    return not strategy.requires or bool(strategy.requires & knowledge_categories)


def select_strategy(
    objections: list[str],
    disabled: set[str],
    rng: random.Random | None = None,
    knowledge_categories: set[str] | None = None,
) -> tuple[Strategy | None, str | None]:
    """
    Objection prioritaire du message, puis tirage au hasard parmi ses stratégies activées ET
    disponibles (informations requises présentes dans la base de connaissances).
    Renvoie (stratégie, None), ou (None, objection) si des stratégies étaient activées mais
    toutes bloquées faute d'informations — la consigne de repli s'applique alors —, ou (None, None).
    """
    known = knowledge_categories or set()
    for objection in OBJECTION_PRIORITY:
        if objection in objections:
            enabled = [s for s in strategies_for(objection) if s.code not in disabled]
            available = [s for s in enabled if is_available(s, known)]
            if available:
                return (rng or _rng).choice(available), None
            if enabled:
                return None, objection  # bloquées faute d'informations : jamais d'invention
            return None, None  # toutes désactivées : Bob répond librement
    return None, None


def strategy_instruction(strategy: Strategy) -> str:
    return f"{strategy.instruction}\n{GUARDRAILS}"
