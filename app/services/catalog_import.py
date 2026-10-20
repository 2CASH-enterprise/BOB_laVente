"""
Import du catalogue au format CSV (section 24, format MVP).
En-têtes attendus (section 24) : SKU, NAME, DESCRIPTION, CATEGORY, PRICE, CURRENCY, STOCK, IMAGE_URL, ACTIVE.
Lot 26 — colonnes facultatives pour les véhicules (français ou anglais) : MARQUE, MODELE, FINITION,
CARROSSERIE, ANNEE, KILOMETRAGE, CARBURANT, BOITE, COULEUR.
"""
import csv
import io
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.repositories.category_repository import CategoryRepository
from app.repositories.product_repository import ProductRepository
from app.schemas.catalog import CsvImportResponse

REQUIRED_COLUMNS = {"SKU", "NAME", "PRICE", "CURRENCY"}
TRUTHY = {"true", "1", "oui", "yes", "vrai"}


CANONICAL = ["SKU", "NAME", "DESCRIPTION", "CATEGORY", "PRICE", "CURRENCY", "STOCK", "IMAGE_URL", "ACTIVE"]


def table_to_import_csv(header: list[str], rows: list[list[str]], keep_vehicle: bool = False,
                        keep_property: bool = False, default_currency: str | None = None) -> str:
    """
    Lot 78 B — un tableau lu dans un fichier Excel ou CSV (séparateur « , » ou « ; ») devient le CSV attendu par
    import_catalog_csv. En-têtes français reconnus (« Nom », « Prix », « Référence »…) ; les colonnes du véhicule, du
    bien ou de la fiche produit gardent leur nom d'origine (« Quantity » reste le stock, pas la contenance). Aucun SKU
    n'est inventé : une ligne sans référence échoue.
    """
    from app.services.csv_column_mapper import _match_column, _vehicle_headers

    property_headers = {}
    if keep_property:
        from app.services.real_estate import csv_property_headers

        property_headers = csv_property_headers(header)
    vehicle_headers = _vehicle_headers(header) if keep_vehicle else {}
    names: list[str] = []
    for original in header:
        original = (original or "").strip()
        exact = original.upper().replace(" ", "_")
        if exact in CANONICAL and exact not in names:
            name = exact
        elif original in property_headers or original in vehicle_headers:
            name = original
        else:
            canonical = _match_column(original)
            name = canonical if canonical and canonical not in names else original
        names.append(name if name not in names else f"{name} ({len(names) + 1})")
    width = len(names)
    if default_currency and "CURRENCY" not in names:
        names.append("CURRENCY")
    price_at = names.index("PRICE") if "PRICE" in names else None
    currency_at = names.index("CURRENCY") if "CURRENCY" in names else None
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(names)
    for row in rows:
        if not any(str(c).strip() for c in row):
            continue
        cells = [str(c) for c in (row + [""] * width)[:width]] + [""] * (len(names) - width)
        if price_at is not None:
            cells[price_at] = _clean_price(cells[price_at])
        if currency_at is not None and default_currency and not cells[currency_at].strip():
            cells[currency_at] = default_currency
        writer.writerow(cells)
    return out.getvalue()


def _clean_price(value: str) -> str:
    """« 1 250,50 € » → « 1250.50 » ; une valeur déjà propre ne change pas."""
    text = re.sub(r"[\s\u00a0\u202f€$]|FCFA|XOF|EUR", "", str(value or ""), flags=re.I)
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    elif "," in text:  # « 1,250.50 »
        text = text.replace(",", "")
    return text


def _has_column(row: dict, key: str) -> bool:
    """Lot 78 B — le fichier a-t-il une colonne pour ce champ de fiche (par son libellé ou sa clé) ?"""
    from app.services import product_sheets

    mapping = product_sheets._header_keys()
    return any(mapping.get(product_sheets._plain(h)) == key for h in row if h)


def _parse_active(raw: str | None) -> bool:
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() in TRUTHY


async def import_catalog_csv(
    db: AsyncSession, tenant_id, csv_content: str, max_new_products: int | None = None,
    with_vehicles: bool = True, without_prices: bool = False, with_properties: bool = False,
    with_sheets: bool = False, default_category: str | None = None,
) -> CsvImportResponse:
    """
    max_new_products : plafond freemium (section produits) — n'empêche jamais la mise à
    jour d'un produit déjà existant, seulement l'ajout de nouveaux au-delà du plan.
    """
    reader = csv.DictReader(io.StringIO(csv_content))

    # Lot 53 — courtier : ni prix ni devise dans le fichier (jamais de prix chez un cabinet d'assurance).
    required = {"SKU", "NAME"} if without_prices else REQUIRED_COLUMNS
    if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
        missing = required - set(reader.fieldnames or [])
        return CsvImportResponse(
            total_rows=0,
            imported=0,
            updated=0,
            failed=0,
            available=0,
            unavailable=0,
            errors=[f"Colonnes obligatoires manquantes : {', '.join(sorted(missing))}"],
        )

    product_repo = ProductRepository(db)
    category_repo = CategoryRepository(db)

    total = imported = updated = failed = available = unavailable = 0
    errors: list[str] = []

    for row_number, row in enumerate(reader, start=2):  # ligne 1 = en-têtes
        total += 1
        sku = (row.get("SKU") or "").strip()
        name = (row.get("NAME") or "").strip()

        if not sku or not name:
            failed += 1
            errors.append(f"Ligne {row_number} : SKU et NAME sont obligatoires")
            continue

        try:
            price = Decimal(0) if without_prices else Decimal(str(row.get("PRICE", "")).strip())
            if price <= 0 and not without_prices:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            failed += 1
            errors.append(f"Ligne {row_number} ({sku}) : prix invalide")
            continue

        try:
            stock = 0 if without_prices else int(str(row.get("STOCK", "0")).strip() or 0)
        except ValueError:
            failed += 1
            errors.append(f"Ligne {row_number} ({sku}) : stock invalide")
            continue

        currency = (row.get("CURRENCY") or "").strip().upper() or ("XOF" if without_prices else "")
        if len(currency) != 3:
            failed += 1
            errors.append(f"Ligne {row_number} ({sku}) : devise invalide (attendu code ISO 3 lettres)")
            continue

        category_id = None
        category_name = (row.get("CATEGORY") or "").strip()
        if category_name:
            category = await category_repo.get_or_create_by_name(tenant_id, category_name)
            category_id = category.id

        active = _parse_active(row.get("ACTIVE"))

        from app.services.vehicle import normalize_vehicle, vehicle_from_csv_row

        # Lot 32 : colonnes véhicule lues seulement pour une concession (ignorées sinon).
        vehicle_columns = vehicle_from_csv_row(row) if with_vehicles else {}
        vehicle, vehicle_errors = normalize_vehicle(vehicle_columns)
        if vehicle_errors:
            failed += 1
            errors.append(f"Ligne {row_number} ({sku}) : {' ; '.join(vehicle_errors)}")
            continue

        bob_category = sheet = None
        has_property_columns = False
        if with_properties:  # lot 79 : agence immobilière — type de bien et fiche lus (ou devinés) dans le fichier
            from app.services.real_estate import property_from_csv_row

            bob_category, sheet, has_property_columns = property_from_csv_row(
                row, name, (row.get("DESCRIPTION") or "").strip() or None)

        sheet_category = sheet_values = None
        has_sheet_columns = has_category_column = False
        if with_sheets:  # lot 78 B : commerce — catégorie Bob et champs de fiche lus dans le fichier
            from app.services import product_sheets

            category_cell = next((v for h, v in row.items() if h and product_sheets.is_category_header(h)), None)
            has_category_column = category_cell is not None
            sheet_category = product_sheets.category_from_value(category_cell)
            if category_cell and sheet_category is None:
                failed += 1
                errors.append(f"Ligne {row_number} ({sku}) : catégorie Bob inconnue « {str(category_cell)[:40]} »")
                continue

        existing = await product_repo.get_by_sku(tenant_id, sku)
        if with_sheets:
            from app.services import product_sheets

            code = sheet_category or (existing.bob_category if existing is not None else None) or (
                default_category if product_sheets.is_category(default_category) else None) or \
                product_sheets.guess_category(name, (row.get("DESCRIPTION") or ""), (row.get("CATEGORY") or ""))
            sheet_category = code
            sheet_values, has_sheet_columns = product_sheets.sheet_from_row(row, code)
        if existing is not None:
            existing.name = name
            existing.description = (row.get("DESCRIPTION") or "").strip() or None
            existing.price = price
            existing.currency = currency
            existing.stock_quantity = stock
            existing.category_id = category_id
            from app.services.photo_studio import reset_for_new_source

            reset_for_new_source(existing, (row.get("IMAGE_URL") or "").strip() or None)  # lot 87
            existing.active = active
            if vehicle is not None:  # un fichier sans colonnes véhicule n'efface pas une fiche existante
                existing.vehicle = vehicle
            if with_sheets and (has_category_column or not existing.bob_category):
                existing.bob_category = sheet_category
            if with_sheets and has_sheet_columns:
                # Les champs présents dans le fichier remplacent ceux de la fiche ; une cellule vide les efface.
                from app.services import product_sheets

                kept = {k: v for k, v in (existing.sheet or {}).items()
                        if k not in {key for _, key in product_sheets.field_columns()} or not _has_column(row, k)}
                existing.sheet = product_sheets.normalize_sheet(existing.bob_category, {**kept, **(sheet_values or {})})
            if with_properties and (has_property_columns or not existing.bob_category):
                existing.bob_category = bob_category  # un fichier sans colonnes de bien n'efface pas une fiche
                if has_property_columns:
                    existing.sheet = sheet
            updated += 1
        else:
            if max_new_products is not None and imported >= max_new_products:
                failed += 1
                errors.append(f"Ligne {row_number} ({sku}) : limite du plan freemium atteinte, produit ignoré")
                continue
            db.add(
                Product(
                    tenant_id=tenant_id,
                    sku=sku,
                    name=name,
                    description=(row.get("DESCRIPTION") or "").strip() or None,
                    price=price,
                    currency=currency,
                    stock_quantity=stock,
                    category_id=category_id,
                    image_url=(row.get("IMAGE_URL") or "").strip() or None,
                    active=active,
                    vehicle=vehicle,
                    bob_category=sheet_category if with_sheets else bob_category,
                    sheet=sheet_values if with_sheets else sheet,
                )
            )
            imported += 1

        if active and stock > 0:
            available += 1
        else:
            unavailable += 1

    await db.commit()

    return CsvImportResponse(
        total_rows=total,
        imported=imported,
        updated=updated,
        failed=failed,
        available=available,
        unavailable=unavailable,
        errors=errors,
    )


# --- Lot 78 B : modèle Excel par secteur (et par catégorie) et export dans le même format, réimportable ----------

VEHICLE_TEMPLATE = [("MARQUE", "brand"), ("MODELE", "model"), ("FINITION", "trim"), ("ANNEE", "year"),
                    ("KILOMETRAGE", "mileage_km"), ("CARBURANT", "fuel"), ("BOITE", "gearbox"),
                    ("CARROSSERIE", "body_type"), ("COULEUR", "color")]


def file_columns(tenant, codes: list[str] | None = None) -> list[tuple[str, str, str]]:
    """(en-tête, nature, clé) des colonnes du fichier du secteur : base, catégorie Bob, fiche, bien ou véhicule."""
    from app.services import product_sheets
    from app.services.business_type import is_dealership, is_insurance, is_real_estate, is_store

    base = ["SKU", "NAME", "DESCRIPTION", "IMAGE_URL", "ACTIVE"] if is_insurance(tenant) else product_sheets.BASE_COLUMNS
    columns = [(h, "base", h) for h in base]
    if is_store(tenant):
        columns.append((product_sheets.CATEGORY_HEADER, "category", ""))
        columns += [(label, "sheet", key) for label, key in product_sheets.field_columns(codes)]
    elif is_real_estate(tenant):
        from app.services.real_estate import TEMPLATE_COLUMNS

        columns += [(h, "property", key) for h, key in TEMPLATE_COLUMNS]
    elif is_dealership(tenant):
        columns += [(h, "vehicle", key) for h, key in VEHICLE_TEMPLATE]
    return columns


def export_cell(product, kind: str, key: str):
    from app.services import product_sheets

    if kind == "base":
        return {"SKU": product.sku, "NAME": product.name, "DESCRIPTION": product.description or "",
                "PRICE": float(product.price or 0), "CURRENCY": product.currency, "STOCK": product.stock_quantity,
                "IMAGE_URL": getattr(product, "image_original_url", None) or product.image_url or "",  # lot 87 : la photo d'origine
                "ACTIVE": "oui" if product.active else "non"}[key]
    if kind == "category":
        return product_sheets.label(product.bob_category) if product_sheets.is_category(product.bob_category) else ""
    if kind == "property" and key == "type":
        return product_sheets.label(product.bob_category) if product_sheets.is_property(product.bob_category) else ""
    source = product.vehicle if kind == "vehicle" else product.sheet
    value = (source or {}).get(key, "")
    return value if isinstance(value, (int, float)) else str(value or "")


TEMPLATE_HELP = [
    ["Une ligne par produit. La 1re ligne (les en-têtes) ne doit pas être modifiée ; les colonnes inutiles peuvent "
     "être supprimées ou laissées vides."],
    ["SKU : votre référence, unique pour chaque produit. Si elle existe déjà dans Bob, le produit est mis à jour."],
    ["NAME : nom du produit. PRICE : prix (ex. 25000 ou 19,90). CURRENCY : devise (XOF, EUR…), celle du compte si vide."],
    ["STOCK : quantité disponible. IMAGE_URL : lien d'une photo. ACTIVE : oui ou non (oui si vide)."],
]


def template_help(tenant) -> list[list[str]]:
    from app.services import product_sheets
    from app.services.business_type import is_insurance, is_real_estate, is_store

    rows = TEMPLATE_HELP[:2] + [["ACTIVE : oui ou non (oui si vide)."]] if is_insurance(tenant) else list(TEMPLATE_HELP)
    if is_store(tenant):
        rows.append([f"{product_sheets.CATEGORY_HEADER} : catégorie Bob du produit, au choix : "
                     + ", ".join(f"{code} ({cat['label']})" for code, cat in product_sheets.CATEGORIES.items())])
        rows.append(["Les autres colonnes remplissent la fiche du produit (ce que Bob répond aux clients). "
                     "Une case vide efface l'information déjà enregistrée."])
    elif is_real_estate(tenant):
        rows.append(["TYPE : Appartement, Maison, Terrain, Local commercial / bureau, Parking / box. "
                     "TRANSACTION : Vente ou Location. DPE et GES : lettre de A à G. MEUBLE : oui ou non."])
    return rows
