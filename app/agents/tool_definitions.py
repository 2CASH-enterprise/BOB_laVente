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
            "paiement, stock incohérent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Motif du transfert"}},
            "required": ["reason"],
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
