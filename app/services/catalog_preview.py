"""
Lot 88 — « Vérifiez avant d'importer » : un catalogue (Excel, CSV ou PDF) est d'abord montré en tableau modifiable.
Les cellules douteuses sont signalées (erreur : la ligne ne sera pas importée ; avertissement : elle le sera, mais
mérite un coup d'œil) ; le commerçant corrige, revérifie, puis importe. La vérification rejoue l'import réel sans rien
enregistrer : les règles sont exactement celles de l'import (prix, devise, stock, catégorie Bob, véhicule, plafond).
"""
import csv
import io

MAX_ROWS = 2000
MAX_COLUMNS = 60
MAX_CELL = 5000

LABELS = {"SKU": "Référence", "NAME": "Nom", "DESCRIPTION": "Description", "CATEGORY": "Rayon", "PRICE": "Prix",
          "CURRENCY": "Devise", "STOCK": "Stock", "IMAGE_URL": "Photo", "ACTIVE": "Actif"}


class PreviewError(ValueError):
    """Tableau refusé (trop grand, vide) : message pour le commerçant."""


def import_options(tenant) -> dict:
    """Options de l'import selon l'activité (identiques pour la vérification et pour l'import réel)."""
    from app.services.business_type import is_dealership, is_insurance, is_real_estate, is_store

    return {"with_vehicles": is_dealership(tenant), "without_prices": is_insurance(tenant),
            "with_properties": is_real_estate(tenant), "with_sheets": is_store(tenant),
            "default_category": tenant.default_bob_category}


def to_canonical(tenant, header: list[str], rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Tableau lu dans le fichier → colonnes de Bob (Référence, Nom, Prix…), prix nettoyés, devise par défaut."""
    from app.services.business_type import is_dealership, is_insurance, is_real_estate
    from app.services.catalog_import import table_to_import_csv

    content = table_to_import_csv(header, rows, keep_vehicle=is_dealership(tenant), keep_property=is_real_estate(tenant),
                                  default_currency=None if is_insurance(tenant) else tenant.currency)
    table = list(csv.reader(io.StringIO(content)))
    return (table[0], table[1:]) if table else ([], [])


def clean_table(header, rows) -> tuple[list[str], list[list[str]]]:
    """Tableau renvoyé par le navigateur : bornes de taille, cellules en texte, lignes vides retirées."""
    if not isinstance(header, list) or not header or not all(isinstance(h, str) and h.strip() for h in header):
        raise PreviewError("Tableau sans colonnes")
    if len(header) > MAX_COLUMNS:
        raise PreviewError(f"Trop de colonnes (au plus {MAX_COLUMNS})")
    if not isinstance(rows, list):
        raise PreviewError("Tableau illisible")
    if len(rows) > MAX_ROWS:
        raise PreviewError(f"Trop de lignes (au plus {MAX_ROWS} par import : découpez le fichier)")
    width = len(header)
    out = []
    for row in rows:
        if not isinstance(row, list):
            raise PreviewError("Tableau illisible")
        cells = [("" if c is None else str(c))[:MAX_CELL] for c in (row + [""] * width)[:width]]
        out.append(cells)
    return [h.strip()[:120] for h in header], out


def to_csv(header: list[str], rows: list[list[str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


async def check(db, tenant, header: list[str], rows: list[list[str]], max_new: int | None) -> list[dict]:
    """Erreurs (ligne refusée) et avertissements, cellule par cellule. Rien n'est enregistré."""
    from app.services.business_type import is_insurance
    from app.services.catalog_import import import_catalog_csv

    options = import_options(tenant)
    tenant_id = tenant.id
    async with db.begin_nested() as savepoint:
        report = await import_catalog_csv(db, tenant_id, to_csv(header, rows), max_new_products=max_new,
                                          dry_run=True, **options)
        await savepoint.rollback()
    issues = list(report.issues)
    if not issues and report.errors:  # colonnes obligatoires absentes : tout le tableau est concerné
        issues = [{"row": None, "field": None, "level": "error", "message": report.errors[0]}]
    blocked = {i["row"] for i in issues if i["level"] == "error"}
    at = {name: index for index, name in enumerate(header)}
    seen: dict[str, int] = {}
    for index, row in enumerate(rows):
        if not any(c.strip() for c in row):
            continue
        sku = row[at["SKU"]].strip() if "SKU" in at else ""
        if sku:
            if sku in seen:
                issues.append({"row": index, "field": "SKU", "level": "warning",
                               "message": f"Même référence que la ligne {seen[sku] + 1} : elle la remplacera"})
            else:
                seen[sku] = index
        if index in blocked:
            continue
        if "STOCK" in at and not is_insurance(tenant) and not row[at["STOCK"]].strip():
            issues.append({"row": index, "field": "STOCK", "level": "warning",
                           "message": "Stock vide : le produit sera présenté comme indisponible"})
        if "IMAGE_URL" in at and not row[at["IMAGE_URL"]].strip():
            issues.append({"row": index, "field": "IMAGE_URL", "level": "warning",
                           "message": "Sans photo : Bob ne pourra pas la montrer à vos clients"})
    return issues


def preview(header: list[str], rows: list[list[str]], issues: list[dict], **extra) -> dict:
    ready = sum(1 for i, row in enumerate(rows) if any(c.strip() for c in row)
                and not any(x["level"] == "error" and x["row"] in (i, None) for x in issues))
    return {"header": header, "labels": [LABELS.get(h, h) for h in header], "rows": rows, "issues": issues,
            "ready": ready, "total": sum(1 for row in rows if any(c.strip() for c in row)), **extra}
