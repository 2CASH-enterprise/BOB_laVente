"""
Lot 75 — « Qu'est-ce que vous avez en stock ? » : test réel du 16/10, trois réponses différentes à la même question
(Bob répondait d'après la fin de la conversation). Bob reçoit désormais un aperçu FIXE du catalogue : les catégories,
le nombre de produits disponibles et quelques noms — jamais de prix (le prix seulement sur demande).
"""
from sqlalchemy import select

from app.models.category import Category
from app.models.product import Product

MAX_CATEGORIES = 12
NAMES_PER_CATEGORY = 3
MAX_NAMES_WITHOUT_CATEGORY = 8
OTHER = "Autres produits"


async def catalog_overview(db, tenant_id) -> str:
    rows = (await db.execute(
        select(Product.name, Category.name)
        .outerjoin(Category, Category.id == Product.category_id)
        .where(Product.tenant_id == tenant_id, Product.active.is_(True), Product.stock_quantity > 0)
        .order_by(Category.name, Product.name)
    )).all()
    if not rows:
        return ""
    groups: dict[str, list[str]] = {}
    for product_name, category_name in rows:
        groups.setdefault(category_name or OTHER, []).append(product_name)
    if list(groups) == [OTHER]:
        names = groups[OTHER]
        more = f" (et {len(names) - MAX_NAMES_WITHOUT_CATEGORY} autres)" if len(names) > MAX_NAMES_WITHOUT_CATEGORY else ""
        body = "- " + ", ".join(names[:MAX_NAMES_WITHOUT_CATEGORY]) + more
    else:
        ordered = sorted(groups.items(), key=lambda kv: (kv[0] == OTHER, -len(kv[1]), kv[0]))
        lines = []
        for category, names in ordered[:MAX_CATEGORIES]:
            sample = ", ".join(names[:NAMES_PER_CATEGORY]) + (", …" if len(names) > NAMES_PER_CATEGORY else "")
            lines.append(f"- {category} ({len(names)} disponible{'s' if len(names) > 1 else ''}) : {sample}")
        if len(ordered) > MAX_CATEGORIES:
            lines.append(f"- et {len(ordered) - MAX_CATEGORIES} autres catégories")
        body = "\n".join(lines)
    return ("\n\nCATALOGUE EN BREF (produits disponibles, sans les prix)\n\n" + body + "\n\n"
            "À une question générale (« qu'est-ce que vous avez ? »), réponds TOUJOURS à partir de cet aperçu : "
            "cite les catégories (ou quelques produits s'il n'y a pas de catégories), sans prix, puis demande ce que "
            "le client cherche. Pour le détail d'un produit, utilise les outils.")
