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

STRATEGY_LIBRARY_VERSION = "v3"  # lot 54 : stratégies du courtier (boutique et concession inchangées)
ONLINE_STORE = "ONLINE_STORE"
CAR_DEALERSHIP = "CAR_DEALERSHIP"
INSURANCE_BROKER = "INSURANCE_BROKER"
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
    "AUCUNE condition (paiement, livraison, retours, remboursement, garantie, délai). Si le client pose "
    "une question précise sur ces conditions, appelle l'outil handoff_to_human (raison : sa question) et "
    "dis-lui que tu transmets sa question à la boutique ; sinon, demande-lui ce qui l'inquiète."
)


# Lot 45 — même consigne, dans le vocabulaire de la concession.
DEALERSHIP_NO_KNOWLEDGE_INSTRUCTION = NO_KNOWLEDGE_INSTRUCTION.replace("la boutique", "la concession")


# Lot 54 — courtier : ce qui manque ne s'invente pas non plus (paiement, conditions, garanties).
INSURANCE_NO_KNOWLEDGE_INSTRUCTION = (
    "Le client pose une question sur des conditions que le cabinet n'a pas renseignées. N'affirme AUCUNE "
    "condition (moyens de paiement, garanties, exclusions, délais, prise en charge). Si le client pose une "
    "question précise, appelle l'outil handoff_to_human (raison : sa question) et dis-lui que tu transmets sa "
    "question au cabinet ; sinon, demande-lui ce qui l'inquiète."
)


def no_knowledge_instruction(business_type: str | None = None) -> str:
    if business_type == INSURANCE_BROKER:
        return INSURANCE_NO_KNOWLEDGE_INSTRUCTION
    return DEALERSHIP_NO_KNOWLEDGE_INSTRUCTION if business_type == "CAR_DEALERSHIP" else NO_KNOWLEDGE_INSTRUCTION


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
    activity: str = ONLINE_STORE  # lot 45 : chaque stratégie appartient à une seule activité


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

# --- Lot 45 : concession automobile ---------------------------------------------------------------------
# Bob ne vend pas la voiture sur WhatsApp : chaque stratégie vise à lever le frein avec des faits RÉELS
# (fiche du véhicule, base de connaissances) et, quand c'est utile, à obtenir un rendez-vous. Jamais de
# valeur de reprise, de taux, de mensualité ni de remise : cela se discute avec un conseiller.
_VISIT = ("propose-lui de venir à la concession : get_available_slots pour lui proposer un créneau, puis "
          "request_appointment quand il en choisit un")
_NO_TRANSFER_FOR_MONEY = ("Ne transfère pas pour cette question (handoff_to_human) : seulement s'il insiste pour "
                          "avoir des chiffres tout de suite.")
_PAPERS_INFO = frozenset({"CONDITIONS", "GARANTIE", "FAQ", "AUTRE"})
_DEALER_TRUST_INFO = frozenset({"ADRESSE", "HORAIRES", "GARANTIE", "PAIEMENT", "CONDITIONS"})


def _auto(code, objection, label, description, instruction, requires=frozenset()):
    return Strategy(code, objection, label, description, instruction, requires=requires, activity=CAR_DEALERSHIP)


STRATEGIES += [
    _auto("AUTO_PRIX_VALEUR", "PRIX_TROP_ELEVE", "Valeur",
          "Bob explique ce qui justifie le prix avec la vraie fiche du véhicule (année, kilométrage, équipements).",
          "Le client trouve le véhicule cher. Explique ce qui justifie ce prix en t'appuyant uniquement sur la fiche "
          "du véhicule renvoyée par search_products (année, kilométrage, carburant, boîte, description). "
          "N'ajoute aucun équipement ni aucun état qui n'y figure pas, et n'annonce aucune remise."),
    _auto("AUTO_PRIX_ALTERNATIVE", "PRIX_TROP_ELEVE", "Alternative",
          "Bob propose un véhicule comparable moins cher, s'il y en a un en stock.",
          "Le client trouve le véhicule cher. Cherche un véhicule comparable moins cher avec search_products et "
          "propose-le s'il existe ; sinon, dis-le simplement."),
    _auto("AUTO_PRIX_BUDGET", "PRIX_TROP_ELEVE", "Budget",
          "Bob demande le budget du client, le note sur sa fiche et cherche ce qui y correspond.",
          "Le client trouve le véhicule cher. Demande-lui poliment quel budget il envisage ; quand il le donne, "
          "note-le avec update_prospect_profile et cherche ce qui y correspond avec search_products."),
    _auto("AUTO_PRIX_FINANCEMENT", "PRIX_TROP_ELEVE", "Paiement étalé",
          "Bob rappelle les solutions de paiement réelles de la concession (base de connaissances).",
          "Le client trouve le véhicule cher. Rappelle-lui les solutions de paiement de la concession, uniquement "
          "telles qu'elles figurent dans la base de connaissances, sans aucun taux ni mensualité qui n'y figure pas. "
          "S'il est intéressé, note-le avec update_prospect_profile (paiement : FINANCEMENT).",
          requires=frozenset({"PAIEMENT"})),
    _auto("AUTO_FIN_SOLUTIONS", "FINANCEMENT", "Solutions de paiement",
          "Bob présente les solutions de paiement renseignées et note l'intérêt pour le financement.",
          "Le client veut un financement ou payer en plusieurs fois. Présente-lui les solutions de paiement de la "
          "concession, uniquement telles qu'elles figurent dans la base de connaissances (aucun taux, aucune "
          "mensualité, aucun accord de crédit qui n'y figure pas). Note son intérêt avec update_prospect_profile "
          f"(paiement : FINANCEMENT), puis {_VISIT}. {_NO_TRANSFER_FOR_MONEY}",
          requires=frozenset({"PAIEMENT"})),
    _auto("AUTO_FIN_VISITE", "FINANCEMENT", "Étude en rendez-vous",
          "Bob note l'intérêt pour le financement et propose un rendez-vous pour l'étudier avec un conseiller.",
          "Le client veut un financement ou payer en plusieurs fois. Ne donne aucun chiffre et n'affirme pas quelles "
          "solutions existent. Note son intérêt avec update_prospect_profile (paiement : FINANCEMENT), explique "
          f"que les possibilités de financement s'étudient avec un conseiller lors d'une visite, et {_VISIT}. "
          f"{_NO_TRANSFER_FOR_MONEY}"),
    _auto("AUTO_REPRISE_ESTIMATION", "REPRISE", "Estimation sur place",
          "Bob note le véhicule à reprendre et propose une estimation à la concession, sans jamais donner de valeur.",
          "Le client parle de reprise de son véhicule. Ne donne JAMAIS de valeur de reprise, même approximative. "
          "Demande-lui la marque, le modèle, l'année et le kilométrage de son véhicule, note-les avec "
          "update_prospect_profile (véhicule à reprendre), et propose une estimation à la concession : get_available_slots, "
          "puis request_appointment (kind : ESTIMATION_REPRISE) quand il choisit un créneau."),
    _auto("AUTO_REPRISE_ATTENTES", "REPRISE", "Comprendre ses attentes",
          "Bob demande ce que le client espère de sa reprise et sur quoi il se base, puis propose l'estimation.",
          "Le client parle de reprise de son véhicule ou trouve la reprise trop basse. Ne donne et ne promets "
          "aucune valeur. Demande-lui ce qu'il espère et sur quoi il se base (état, entretien, kilométrage), "
          "note ce qu'il dit sur son véhicule avec update_prospect_profile (véhicule à reprendre), et explique que la valeur "
          "est fixée après inspection du véhicule à la concession ; propose une estimation (get_available_slots)."),
    _auto("AUTO_CONFIANCE_FAITS", "CONFIANCE", "Rassurer par les faits",
          "Bob rappelle les faits réels de la concession : adresse, horaires, garanties, conditions.",
          "Le client doute du sérieux de la concession ou de l'annonce. Rassure-le uniquement avec des faits réels de "
          "la base de connaissances (adresse de la concession, horaires, garanties, conditions de paiement). Ne "
          "demande jamais d'acompte. Si une information n'y figure pas, ne l'affirme pas.",
          requires=_DEALER_TRUST_INFO),
    _auto("AUTO_CONFIANCE_VOIR", "CONFIANCE", "Venir voir / essayer",
          "Bob propose de venir voir et essayer le véhicule avant tout engagement.",
          "Le client doute du sérieux de la concession ou de l'annonce. Propose-lui de venir voir et essayer le "
          "véhicule à la concession avant tout engagement, sans rien payer à l'avance : get_available_slots pour "
          "lui proposer un créneau, puis request_appointment (kind : ESSAI) quand il en choisit un."),
    _auto("AUTO_CONFIANCE_COMPRENDRE", "CONFIANCE", "Comprendre",
          "Bob demande ce qui inquiète précisément le client, pour y répondre.",
          "Le client doute du sérieux de la concession ou de l'annonce. Demande-lui ce qui l'inquiète précisément "
          "(le véhicule, les papiers, le paiement), pour pouvoir lui répondre avec des faits."),
    _auto("AUTO_PAPIERS_FAITS", "PAPIERS", "Répondre par les faits",
          "Bob répond sur les papiers uniquement avec ce que la concession a renseigné.",
          "Le client doute des papiers du véhicule (carte grise, dédouanement, importation). Réponds uniquement "
          "avec ce qui figure dans la fiche du véhicule (search_products) ou dans la base de connaissances. Si sa "
          "question précise n'y trouve pas de réponse, n'affirme rien : appelle handoff_to_human (raison : sa "
          "question sur les papiers) et dis-lui que tu transmets sa question à la concession.",
          requires=_PAPERS_INFO),
    _auto("AUTO_PAPIERS_VOIR", "PAPIERS", "Voir sur place",
          "Bob ne garantit rien et propose de venir voir le véhicule et ses documents avec un conseiller.",
          "Le client doute des papiers du véhicule (carte grise, dédouanement, importation). N'affirme rien sur les "
          "papiers qui ne figure pas dans la fiche du véhicule. S'il pose une question précise (« il est "
          "dédouané ? »), appelle handoff_to_human (raison : sa question) et dis-lui que tu transmets sa question "
          f"à la concession ; sinon, explique qu'il pourra consulter les documents sur place et {_VISIT}."),
    _auto("AUTO_ETAT_FICHE", "ETAT_VEHICULE", "Fiche du véhicule",
          "Bob donne les informations réelles de la fiche (kilométrage, année, description), sans rien ajouter.",
          "Le client doute de l'état du véhicule. Donne-lui les informations réelles de sa fiche (search_products : "
          "kilométrage, année, description). N'affirme JAMAIS qu'il n'a pas eu d'accident, que le kilométrage est "
          "certifié ou que l'entretien est à jour si ce n'est pas écrit dans la fiche : dis honnêtement que tu n'as "
          "pas ce détail."),
    _auto("AUTO_ETAT_ESSAI", "ETAT_VEHICULE", "Venir inspecter / essayer",
          "Bob propose de venir inspecter et essayer le véhicule.",
          "Le client doute de l'état du véhicule. N'affirme rien qui ne figure pas dans sa fiche, et propose-lui "
          "de venir l'inspecter et l'essayer à la concession : get_available_slots pour lui proposer un créneau, "
          "puis request_appointment (kind : ESSAI) quand il en choisit un."),
    _auto("AUTO_DELAI_ALTERNATIVE", "DELAI", "Véhicule disponible",
          "Bob propose un véhicule comparable disponible tout de suite, s'il y en a un.",
          "Le client trouve le délai trop long. Propose un véhicule comparable disponible tout de suite s'il en "
          "existe un (search_products) ; sinon, indique honnêtement le délai tel qu'il figure dans la base de "
          "connaissances, sans en inventer."),
    _auto("AUTO_RUPTURE_SIMILAIRE", "RUPTURE_STOCK", "Véhicule similaire",
          "Bob propose le véhicule disponible le plus proche et note la recherche du client.",
          "Le véhicule voulu n'est plus disponible. Propose le véhicule disponible le plus proche (search_products), "
          "et note ce que cherche le client avec update_prospect_profile (besoin)."),
    _auto("AUTO_HESITATION_CLARIFIER", "HESITATION", "Clarifier",
          "Bob cherche ce qui fait hésiter : le prix, le financement, le modèle ou la reprise ?",
          "Le client hésite. Demande-lui, avec une seule question simple, ce qui le fait hésiter : plutôt le prix, "
          "le financement, le modèle ou la reprise de son véhicule ? Propose ensuite ton aide sur ce point."),
    _auto("AUTO_HESITATION_RESPECTER", "HESITATION", "Respecter",
          "Bob prend acte, résume le véhicule en une phrase et reste disponible, sans insister.",
          "Le client hésite. Respecte sa décision : résume en une phrase le véhicule qui l'intéressait et indique "
          "que tu restes disponible. N'insiste pas et ne pose pas de question."),
    _auto("AUTO_HESITATION_ESSAI", "HESITATION", "Essai sans engagement",
          "Bob propose un essai sans engagement pour l'aider à se décider.",
          "Le client hésite. Propose-lui, une seule fois et sans insister, de venir essayer le véhicule sans "
          "engagement pour se faire une idée : get_available_slots pour lui proposer un créneau s'il accepte."),
]

# --- Lot 54 : courtier / agent d'assurance ---------------------------------------------------------------
# Bob ne vend pas le contrat et ne donne aucun prix : chaque stratégie lève le frein avec des faits RÉELS
# (produits du cabinet, base de connaissances, informations du cabinet) et vise une demande de cotation
# transmise ou un appel / rendez-vous avec un conseiller. Règlement CIMA : jamais de crédit ni de paiement
# différé, jamais de promesse de prise en charge, jamais de dénigrement d'un autre assureur.
_CALL = ("propose-lui qu'un conseiller l'appelle ou un rendez-vous au cabinet : get_available_slots pour lui "
         "proposer un créneau, puis request_appointment (APPEL ou CABINET) quand il en choisit un")
_INSURANCE_TRUST_INFO = frozenset({"ADRESSE", "HORAIRES", "CONDITIONS", "FAQ", "AUTRE"})


def _assu(code, objection, label, description, instruction, requires=frozenset()):
    return Strategy(code, objection, label, description, instruction, requires=requires, activity=INSURANCE_BROKER)


STRATEGIES += [
    _assu("ASSU_PRIX_ADAPTER", "PRIX_TROP_ELEVE", "Adapter les garanties",
          "Bob explique que le prix dépend des garanties choisies et demande ce qui compte le plus pour le client.",
          "Le client trouve l'assurance chère. Ne donne AUCUN montant. Explique simplement que le prix dépend des "
          "garanties choisies et que le conseiller peut lui proposer une formule adaptée à son budget. Demande-lui ce "
          "qui compte le plus pour lui (les garanties essentielles, une protection plus complète…), note sa réponse "
          f"avec update_insurance_request (couverture souhaitée), puis {_CALL}."),
    _assu("ASSU_PRIX_VALEUR", "PRIX_TROP_ELEVE", "Ce qui est protégé",
          "Bob rappelle ce que l'assurance protège concrètement, d'après la description du produit.",
          "Le client trouve l'assurance chère. Ne donne AUCUN montant. Rappelle-lui en une ou deux phrases ce que "
          "l'assurance protège concrètement, uniquement d'après la description du produit (search_products) ; "
          "n'ajoute aucune garantie qui n'y figure pas. Propose ensuite une proposition personnalisée, sans engagement."),
    _assu("ASSU_PRIX_BUDGET", "PRIX_TROP_ELEVE", "Budget",
          "Bob demande le budget envisagé et le transmet au conseiller, sans jamais le commenter.",
          "Le client trouve l'assurance chère. Demande-lui poliment quel budget il envisage, pour que le conseiller "
          "lui propose ce qui y correspond. Quand il le donne, note-le avec update_insurance_request (budget) SANS "
          "répéter le montant dans ta réponse et sans dire s'il suffit."),
    _assu("ASSU_DEJA_ECHEANCE", "DEJA_ASSURE", "Préparer l'échéance",
          "Bob note l'assureur actuel, l'échéance et la durée du contrat, pour préparer une proposition à temps.",
          "Le client a déjà une assurance. Ne critique jamais son assureur actuel. Demande-lui la date d'échéance de "
          "son contrat et sa durée (mensuel, trimestriel, semestriel ou annuel), note-les avec update_insurance_request "
          "(assureur actuel, échéance, durée), et explique que le conseiller peut lui préparer une proposition avant "
          "l'échéance, sans engagement, pour qu'il puisse comparer."),
    _assu("ASSU_DEJA_BILAN", "DEJA_ASSURE", "Vérifier sa couverture",
          "Bob propose au client de vérifier avec un conseiller s'il est bien couvert, sans engagement.",
          "Le client a déjà une assurance. Ne critique jamais son assureur actuel. Demande-lui ce que couvre son "
          "contrat actuel, et propose-lui de faire le point avec un conseiller pour vérifier qu'il est bien couvert, "
          f"sans engagement : {_CALL}."),
    _assu("ASSU_CONFIANCE_FAITS", "CONFIANCE", "Rassurer par les faits",
          "Bob rappelle les faits réels du cabinet : statut, agrément, adresse, horaires, conditions.",
          "Le client doute du sérieux de l'assurance ou du cabinet. Rassure-le uniquement avec des faits réels : "
          "les INFORMATIONS DU CABINET (statut, compagnie, numéro d'agrément) et la base de connaissances (adresse, "
          "horaires, conditions). Ne promets JAMAIS qu'un sinistre sera pris en charge : explique que les garanties et "
          "les exclusions sont écrites dans le contrat et que le conseiller les lui présente avant tout engagement.",
          requires=_INSURANCE_TRUST_INFO),
    _assu("ASSU_CONFIANCE_CONSEILLER", "CONFIANCE", "Rencontrer un conseiller",
          "Bob propose de rencontrer un conseiller qui présente les garanties et exclusions avant tout engagement.",
          "Le client doute du sérieux de l'assurance ou du cabinet. Ne promets JAMAIS qu'un sinistre sera pris en "
          "charge. Explique qu'un conseiller lui présentera, avant tout engagement, ce que le contrat couvre et ne "
          f"couvre pas, et qu'il pourra poser toutes ses questions ; {_CALL}."),
    _assu("ASSU_CONFIANCE_COMPRENDRE", "CONFIANCE", "Comprendre",
          "Bob demande ce qui inquiète précisément le client, pour y répondre.",
          "Le client doute du sérieux de l'assurance ou du cabinet. Demande-lui ce qui l'inquiète précisément (une "
          "mauvaise expérience, la prise en charge d'un sinistre, le cabinet), pour pouvoir lui répondre avec des faits."),
    _assu("ASSU_BESOIN_SITUATION", "PAS_BESOIN", "Partir de sa situation",
          "Bob pose une question sur la situation du client pour voir ce qui est réellement exposé, sans dramatiser.",
          "Le client ne voit pas l'utilité de s'assurer. Ne fais jamais peur et n'invente aucun risque. Pose-lui une "
          "seule question sur sa situation (famille, véhicule, logement, activité) pour comprendre ce qui compte pour "
          "lui ; si un produit du cabinet y répond, présente-le en une phrase, d'après sa description."),
    _assu("ASSU_BESOIN_RESPECTER", "PAS_BESOIN", "Respecter",
          "Bob prend acte et reste disponible ; il rappelle seulement une obligation légale si le produit l'indique.",
          "Le client ne voit pas l'utilité de s'assurer. Respecte son avis : si la description du produit indique "
          "qu'une garantie est obligatoire (par exemple la responsabilité civile automobile), rappelle-le simplement, "
          "sans menace ; sinon, indique que tu restes disponible. N'insiste pas."),
    _assu("ASSU_COMPARER_PROPOSITION", "COMPARAISON", "Comparer sans engagement",
          "Bob encourage la comparaison et propose une proposition écrite du cabinet pour comparer.",
          "Le client veut comparer. Encourage-le : c'est normal. Ne critique jamais un autre assureur et ne donne "
          "aucun prix. Propose-lui de recevoir une proposition personnalisée du cabinet, sans engagement, pour "
          "comparer sur des bases claires : continue à réunir les informations utiles (update_insurance_request)."),
    _assu("ASSU_COMPARER_CRITERES", "COMPARAISON", "Critères de choix",
          "Bob aide le client à comparer sur les bons critères : garanties, exclusions, franchise, service sinistre.",
          "Le client veut comparer. Aide-le, sans aucun chiffre, à comparer sur les bons critères : les garanties "
          "incluses, les exclusions, la franchise, l'assistance et la gestion des sinistres. Demande-lui ce qui compte "
          f"le plus pour lui, note-le (update_insurance_request, couverture souhaitée), puis {_CALL}."),
    _assu("ASSU_PAIEMENT_CONSEILLER", "PAIEMENT", "Voir avec le conseiller",
          "Bob note le souhait du client ; les modalités de paiement sont présentées par le conseiller.",
          "Le client veut payer la prime en plusieurs fois, plus tard ou à crédit. Ne promets JAMAIS de crédit, de "
          "paiement différé ni de paiement en plusieurs fois : le cabinet n'accorde pas de crédit. Note son souhait "
          "avec update_insurance_request (souhait pour le paiement) et explique que le conseiller lui présentera les "
          f"modalités de paiement possibles avec sa proposition ; {_CALL}."),
    _assu("ASSU_PAIEMENT_FAITS", "PAIEMENT", "Moyens acceptés",
          "Bob cite uniquement les moyens de paiement renseignés par le cabinet, jamais de crédit.",
          "Le client veut payer la prime en plusieurs fois, plus tard ou à crédit. Ne promets JAMAIS de crédit ni de "
          "paiement différé. Cite uniquement les moyens et modalités de paiement figurant dans la base de "
          "connaissances, sans rien ajouter, et note son souhait avec update_insurance_request (souhait pour le "
          "paiement). Le conseiller confirmera les modalités avec la proposition.",
          requires=frozenset({"PAIEMENT"})),
    _assu("ASSU_HESITATION_CLARIFIER", "HESITATION", "Clarifier",
          "Bob cherche ce qui fait hésiter : le prix, les garanties ou la confiance ?",
          "Le client hésite. Demande-lui, avec une seule question simple, ce qui le fait hésiter : plutôt le prix, ce "
          "que couvre l'assurance, ou la confiance ? Propose ensuite ton aide sur ce point."),
    _assu("ASSU_HESITATION_RESPECTER", "HESITATION", "Respecter",
          "Bob prend acte, résume le besoin en une phrase et reste disponible, sans insister.",
          "Le client hésite. Respecte sa décision : résume en une phrase son besoin d'assurance et indique que tu "
          "restes disponible. N'insiste pas et ne pose pas de question."),
    _assu("ASSU_HESITATION_APPEL", "HESITATION", "Appel sans engagement",
          "Bob propose, une seule fois, un court appel sans engagement avec un conseiller.",
          "Le client hésite. Propose-lui, une seule fois et sans insister, un court appel sans engagement avec un "
          "conseiller pour répondre à ses questions : get_available_slots pour lui proposer un créneau s'il accepte."),
]

STRATEGIES_BY_CODE = {s.code: s for s in STRATEGIES}

# Une seule objection traitée par message : la plus bloquante d'abord.
OBJECTION_PRIORITY = ["CONFIANCE", "RUPTURE_STOCK", "PRIX_TROP_ELEVE", "FRAIS_LIVRAISON", "DELAI", "QUALITE", "HESITATION"]
DEALERSHIP_OBJECTION_PRIORITY = [
    "CONFIANCE", "PAPIERS", "ETAT_VEHICULE", "RUPTURE_STOCK", "FINANCEMENT", "PRIX_TROP_ELEVE", "REPRISE", "DELAI", "HESITATION",
]
INSURANCE_OBJECTION_PRIORITY = [
    "CONFIANCE", "PAS_BESOIN", "PAIEMENT", "PRIX_TROP_ELEVE", "DEJA_ASSURE", "COMPARAISON", "HESITATION",
]


def _activity(business_type: str | None) -> str:
    if business_type in (CAR_DEALERSHIP, INSURANCE_BROKER):
        return business_type
    return ONLINE_STORE


def priority_for(business_type: str | None = None) -> list[str]:
    return {CAR_DEALERSHIP: DEALERSHIP_OBJECTION_PRIORITY,
            INSURANCE_BROKER: INSURANCE_OBJECTION_PRIORITY}.get(_activity(business_type), OBJECTION_PRIORITY)

_rng = random.Random()


def strategies_for(objection: str, business_type: str | None = None) -> list[Strategy]:
    activity = _activity(business_type)
    return [s for s in STRATEGIES if s.objection == objection and s.activity == activity]


def is_available(strategy: Strategy, knowledge_categories: set[str]) -> bool:
    return not strategy.requires or bool(strategy.requires & knowledge_categories)


def select_strategy(
    objections: list[str],
    disabled: set[str],
    rng: random.Random | None = None,
    knowledge_categories: set[str] | None = None,
    business_type: str | None = None,
) -> tuple[Strategy | None, str | None]:
    """
    Objection prioritaire du message, puis tirage au hasard parmi ses stratégies activées ET
    disponibles (informations requises présentes dans la base de connaissances).
    Renvoie (stratégie, None), ou (None, objection) si des stratégies étaient activées mais
    toutes bloquées faute d'informations — la consigne de repli s'applique alors —, ou (None, None).
    """
    known = knowledge_categories or set()
    for objection in priority_for(business_type):
        if objection in objections:
            enabled = [s for s in strategies_for(objection, business_type) if s.code not in disabled]
            available = [s for s in enabled if is_available(s, known)]
            if available:
                return (rng or _rng).choice(available), None
            if enabled:
                return None, objection  # bloquées faute d'informations : jamais d'invention
            return None, None  # toutes désactivées : Bob répond librement
    return None, None


# Lot 54 — garde-fous du courtier (règlement CIMA : ni prix, ni promesse de prise en charge, ni crédit).
INSURANCE_GUARDRAILS = (
    "Garde-fous : n'invente jamais de garantie, de condition ni d'avis client ; ne donne jamais de prime, de tarif "
    "ni de montant ; ne promets jamais qu'un sinistre sera pris en charge ; ne propose jamais de crédit ni de "
    "paiement différé ; ne critique aucun assureur ; pas de fausse urgence ; n'insiste pas si le client refuse "
    "clairement."
)


def strategy_instruction(strategy: Strategy) -> str:
    guardrails = INSURANCE_GUARDRAILS if strategy.activity == INSURANCE_BROKER else GUARDRAILS
    return f"{strategy.instruction}\n{guardrails}"
