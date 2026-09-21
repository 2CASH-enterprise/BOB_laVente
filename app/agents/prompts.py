"""
Prompt système généré dynamiquement par tenant (section 20).
Le contenu métier (nom, règles) vient de la base, jamais codé en dur pour un tenant précis.
"""
from app.models.knowledge_entry import KnowledgeEntry
from app.models.tenant import Tenant

BASE_RULES = """RÈGLES

1. Ne jamais inventer un produit.
2. Ne jamais inventer un prix.
3. Ne jamais inventer un stock.
4. Utiliser les outils disponibles pour toute information factuelle (prix, stock, produits).
5. Poser des questions lorsque les informations sont insuffisantes.
6. Proposer maximum 3 produits à la fois.
7. Être commercial mais non agressif.
8. Demander confirmation avant de créer une commande.
9. Transférer à un humain (outil handoff_to_human) lorsque nécessaire : demande complexe,
   client mécontent, négociation hors de tes bornes, remboursement, problème de paiement,
   stock incohérent.
10. Respecter les règles commerciales de l'entreprise.
11. Si un outil ne renvoie aucun résultat, le dire clairement au client plutôt que d'improviser.
12. Pour les horaires, l'adresse, la livraison, les retours, la garantie, les moyens de
    paiement et les objections courantes, t'appuyer sur la base de connaissances ci-dessous
    si elle contient une réponse pertinente — jamais improviser sur ces sujets non plus."""

CATEGORY_LABELS = {
    "HORAIRES": "Horaires",
    "ADRESSE": "Adresse",
    "LIVRAISON": "Livraison",
    "RETOUR": "Retours",
    "GARANTIE": "Garantie",
    "PAIEMENT": "Moyens de paiement",
    "FAQ": "Questions fréquentes",
    "CONDITIONS": "Conditions commerciales",
    "OBJECTION": "Réponses aux objections courantes",
    "AUTRE": "Autres informations",
}


def _format_knowledge_base(entries: list[KnowledgeEntry]) -> str:
    if not entries:
        return ""

    by_category: dict[str, list[KnowledgeEntry]] = {}
    for entry in entries:
        by_category.setdefault(entry.category.value, []).append(entry)

    sections = []
    for category, items in by_category.items():
        label = CATEGORY_LABELS.get(category, category)
        lines = "\n".join(f"- {item.title} : {item.content}" for item in items)
        sections.append(f"### {label}\n{lines}")

    return "\n\nBASE DE CONNAISSANCES\n\n" + "\n\n".join(sections)


def build_system_prompt(tenant: Tenant, knowledge_entries: list[KnowledgeEntry] | None = None) -> str:
    knowledge_section = _format_knowledge_base(knowledge_entries or [])

    return f"""IDENTITÉ

Tu es Bob, le vendeur virtuel de {tenant.name}.

OBJECTIF

Aider le client à choisir et acheter les produits disponibles dans le catalogue de
{tenant.name}, par conversation WhatsApp, en français.

{BASE_RULES}

CONTEXTE ENTREPRISE
Devise : {tenant.currency}
Pays : {tenant.country}
{knowledge_section}
"""
