"""
Import du catalogue au format CSV (section 24, format MVP).
En-têtes attendus (section 24) : SKU, NAME, DESCRIPTION, CATEGORY, PRICE, CURRENCY, STOCK, IMAGE_URL, ACTIVE.
"""
import csv
import io
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.repositories.category_repository import CategoryRepository
from app.repositories.product_repository import ProductRepository
from app.schemas.catalog import CsvImportResponse

REQUIRED_COLUMNS = {"SKU", "NAME", "PRICE", "CURRENCY"}
TRUTHY = {"true", "1", "oui", "yes", "vrai"}


def _parse_active(raw: str | None) -> bool:
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() in TRUTHY


async def import_catalog_csv(db: AsyncSession, tenant_id, csv_content: str) -> CsvImportResponse:
    reader = csv.DictReader(io.StringIO(csv_content))

    if reader.fieldnames is None or not REQUIRED_COLUMNS.issubset(set(reader.fieldnames)):
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
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
            price = Decimal(str(row.get("PRICE", "")).strip())
            if price <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            failed += 1
            errors.append(f"Ligne {row_number} ({sku}) : prix invalide")
            continue

        try:
            stock = int(str(row.get("STOCK", "0")).strip() or 0)
        except ValueError:
            failed += 1
            errors.append(f"Ligne {row_number} ({sku}) : stock invalide")
            continue

        currency = (row.get("CURRENCY") or "").strip().upper()
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

        existing = await product_repo.get_by_sku(tenant_id, sku)
        if existing is not None:
            existing.name = name
            existing.description = (row.get("DESCRIPTION") or "").strip() or None
            existing.price = price
            existing.currency = currency
            existing.stock_quantity = stock
            existing.category_id = category_id
            existing.image_url = (row.get("IMAGE_URL") or "").strip() or None
            existing.active = active
            updated += 1
        else:
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
