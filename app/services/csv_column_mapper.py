"""
Mapping automatique de colonnes CSV pour la démo instantanée. Le prospect ne doit
jamais avoir à respecter un format Excel imposé — on reconnaît les synonymes usuels
et on génère les colonnes obligatoires manquantes (SKU, CURRENCY) plutôt que de
rejeter le fichier.
"""
import csv
import io
import unicodedata

SYNONYMS = {
    "NAME": {"nom", "produit", "article", "designation", "name", "product", "item", "title", "titre"},
    "PRICE": {"prix", "price", "prix de vente", "prix vente", "montant", "tarif", "prixvente"},
    "STOCK": {"stock", "qte", "quantite", "quantity", "qty", "disponible"},
    "DESCRIPTION": {"description", "desc", "details", "detail"},
    "CATEGORY": {"categorie", "category", "type", "rubrique"},
    "IMAGE_URL": {"image", "image_url", "photo", "img", "imageurl"},
    "CURRENCY": {"devise", "currency", "monnaie"},
    "ACTIVE": {"actif", "active", "status", "statut"},
    "SKU": {"sku", "ref", "reference", "code", "code produit"},
}


def _normalize(header: str) -> str:
    """Minuscule, sans accents, sans espaces superflus — pour comparer sans se soucier de la casse/accents."""
    stripped = unicodedata.normalize("NFKD", header).encode("ascii", "ignore").decode("ascii")
    return stripped.strip().lower()


def _match_column(header: str) -> str | None:
    normalized = _normalize(header)
    for canonical, synonyms in SYNONYMS.items():
        if normalized in synonyms:
            return canonical
    return None


def map_csv_to_canonical_format(raw_csv: str, default_currency: str) -> str:
    """
    Convertit un CSV quelconque (en-têtes libres) vers le format attendu par
    import_catalog_csv (section 24) : SKU, NAME, DESCRIPTION, CATEGORY, PRICE,
    CURRENCY, STOCK, IMAGE_URL, ACTIVE. Génère un SKU si absent, remplit la devise
    par défaut si absente — jamais un rejet pour un simple problème de format.
    """
    reader = csv.DictReader(io.StringIO(raw_csv))
    if reader.fieldnames is None:
        raise ValueError("Fichier CSV vide ou illisible")

    column_mapping: dict[str, str] = {}
    for header in reader.fieldnames:
        canonical = _match_column(header)
        if canonical and canonical not in column_mapping.values():
            column_mapping[header] = canonical

    output = io.StringIO()
    fieldnames = ["SKU", "NAME", "DESCRIPTION", "CATEGORY", "PRICE", "CURRENCY", "STOCK", "IMAGE_URL", "ACTIVE"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for i, row in enumerate(reader, start=1):
        mapped = {canonical: row.get(original, "") for original, canonical in column_mapping.items()}
        writer.writerow(
            {
                "SKU": mapped.get("SKU") or f"DEMO-{i:04d}",
                "NAME": mapped.get("NAME", ""),
                "DESCRIPTION": mapped.get("DESCRIPTION", ""),
                "CATEGORY": mapped.get("CATEGORY", ""),
                "PRICE": mapped.get("PRICE", ""),
                "CURRENCY": mapped.get("CURRENCY") or default_currency,
                "STOCK": mapped.get("STOCK") or "0",
                "IMAGE_URL": mapped.get("IMAGE_URL", ""),
                "ACTIVE": mapped.get("ACTIVE") or "true",
            }
        )

    return output.getvalue()
