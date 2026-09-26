"""
Définitions des outils exposés au LLM (section 14-19).
Format compatible avec l'API Anthropic Messages (tool use).
"""

TOOL_DEFINITIONS = [
    {
        "name": "search_products",
        "description": (
            "Recherche des produits dans le catalogue de l'entreprise. "
            "Retourne uniquement des produits réels avec leur prix et stock actuels. "
            "N'invente jamais un produit qui n'apparaît pas dans le résultat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Terme de recherche (nom du produit)"},
                "category": {"type": "string", "description": "Nom de catégorie, optionnel"},
                "min_price": {"type": "number", "description": "Prix minimum, optionnel"},
                "max_price": {"type": "number", "description": "Prix maximum (budget du client), optionnel"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_stock",
        "description": "Vérifie le stock RÉEL d'un produit précis par son identifiant. Jamais à deviner.",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string", "description": "UUID du produit"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "get_product_price",
        "description": "Retourne le prix RÉEL et actuel d'un produit. Source de vérité unique pour le prix (section 16).",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string", "description": "UUID du produit"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "recommend_products",
        "description": (
            "Recommande jusqu'à 3 produits selon le besoin exprimé, un budget et/ou une catégorie. "
            "Basé uniquement sur les produits réellement disponibles en catalogue (section 17)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_need": {"type": "string", "description": "Besoin exprimé par le client, en quelques mots"},
                "budget": {"type": "number", "description": "Budget maximum, optionnel"},
                "category": {"type": "string", "description": "Catégorie ciblée, optionnel"},
            },
            "required": ["customer_need"],
        },
    },
    {
        "name": "handoff_to_human",
        "description": (
            "Transfère la conversation à un vendeur humain (section 19). À utiliser pour : demande "
            "complexe, client mécontent, négociation hors de tes limites, remboursement, problème de "
            "paiement, stock incohérent. Uniquement si le DERNIER message du client le justifie — "
            "jamais pour une demande ancienne de l'historique, ni en réponse à une simple salutation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Motif du transfert"}},
            "required": ["reason"],
        },
    },
    {
        "name": "get_frequently_bought_together",
        "description": (
            "Retourne jusqu'à 3 produits fréquemment achetés avec le produit donné, calculés "
            "à partir des vraies commandes passées (pas une configuration manuelle, contrairement "
            "à suggest_complementary_products). Utile quand aucune liaison n'a été configurée "
            "par l'entreprise, ou pour renforcer une suggestion avec des données de vente réelles."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string", "description": "UUID du produit"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "suggest_complementary_products",
        "description": (
            "Suggère jusqu'à 2 produits complémentaires configurés par l'entreprise pour un produit "
            "donné (section 22, upselling). À utiliser une fois que le client a montré un intérêt "
            "clair pour un produit ou vient de le commander — jamais plus de 1 à 2 propositions, "
            "jamais de manière insistante. S'il n'y a aucun complémentaire configuré, ne rien proposer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string", "description": "UUID du produit principal"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "record_marketing_consent",
        "description": (
            "Enregistre la réponse d'un client à une question de consentement marketing que "
            "TU viens de lui poser explicitement (ex. « Acceptez-vous de recevoir nos offres "
            "et promotions ? »). N'appelle cet outil qu'après avoir reçu une réponse claire "
            "(oui/non) — jamais en devinant, jamais sans avoir explicitement posé la question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"accepted": {"type": "boolean", "description": "True si le client a accepté, False s'il a refusé"}},
            "required": ["accepted"],
        },
    },
    {
        "name": "share_payment_link",
        "description": (
            "Renvoie le lien de paiement de l'entreprise (Wave, Orange Money, etc.), si "
            "elle en a configuré un. N'utilise cet outil QUE si le client demande "
            "explicitement comment payer — jamais de façon proactive."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "update_customer_profile",
        "description": (
            "Enregistre une information concrète que le client vient de donner sur lui-même "
            "ou son besoin (nom, ville, ce qu'il cherche, marque préférée, budget). Appelle "
            "cet outil dès qu'une information nouvelle et concrète apparaît dans la conversation "
            "— jamais à chaque message, jamais une supposition de ta part. Ne fournis que les "
            "champs réellement mentionnés par le client, laisse les autres vides."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "first_name": {"type": "string", "description": "Prénom du client, si mentionné"},
                "city": {"type": "string", "description": "Ville du client, si mentionnée"},
                "need": {"type": "string", "description": "Ce que le client recherche (ex. 'smartphone', 'robe de soirée')"},
                "brand": {"type": "string", "description": "Marque préférée mentionnée, si applicable"},
                "budget_max": {"type": "number", "description": "Budget maximum mentionné par le client"},
            },
            "required": [],
        },
    },
    {
        "name": "check_order_status",
        "description": (
            "Renvoie le statut réel d'une commande du client (paiement, livraison, articles). "
            "Sans order_id, renvoie la commande la plus récente de ce client dans cette "
            "conversation. Utilise TOUJOURS cet outil quand un client demande où en est sa "
            "commande — ne réponds jamais de mémoire ni en devinant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "UUID de la commande, optionnel"}},
            "required": [],
        },
    },
    {
        "name": "negotiate_price",
        "description": (
            "Négocie le prix d'un produit avec le client (section 52). Appelle cet outil dès "
            "qu'un client propose un prix inférieur au catalogue. Ne baisse JAMAIS le prix "
            "toi-même dans ta réponse — utilise toujours cet outil, qui applique les règles "
            "de l'entreprise et calcule un plancher réel. Si la décision est COUNTER, propose "
            "exactement le proposed_price renvoyé, jamais un autre chiffre. Si ESCALATE_HUMAN, "
            "informe le client qu'un conseiller va reprendre la main."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "description": "UUID du produit"},
                "customer_offer": {"type": "number", "description": "Prix proposé par le client"},
            },
            "required": ["product_id", "customer_offer"],
        },
    },
    {
        "name": "create_order",
        "description": (
            "Crée réellement une commande. N'appelle CET OUTIL QU'APRÈS que le client a confirmé "
            "explicitement (section 18) — jamais avant, jamais sur une simple hésitation. Le stock "
            "et les prix sont revérifiés en base au moment de l'appel : si le stock a changé depuis "
            "la dernière recherche, la commande peut être refusée, informe-en le client."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Articles commandés",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "string", "description": "UUID du produit"},
                            "quantity": {"type": "integer", "description": "Quantité, minimum 1"},
                        },
                        "required": ["product_id", "quantity"],
                    },
                },
                "delivery_address": {"type": "string", "description": "Adresse de livraison, si fournie"},
                "payment_method": {"type": "string", "description": "Moyen de paiement mentionné par le client"},
            },
            "required": ["items"],
        },
    },
]
