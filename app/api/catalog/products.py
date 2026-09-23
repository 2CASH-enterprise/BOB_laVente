from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.repositories.product_repository import ProductRepository
from app.schemas.catalog import ProductCreate, ProductResponse, ProductUpdate
from app.models.product import Product

router = APIRouter(prefix="/api/v1/products", tags=["products"])


@router.post("", response_model=ProductResponse, status_code=201, dependencies=[Depends(require_role("MANAGER"))])
async def create_product(
    payload: ProductCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.tenant import Tenant
    from app.services.plan_limits import FREEMIUM_MAX_PRODUCTS, remaining_product_slots

    tenant = await db.get(Tenant, current_user.tenant_id)
    remaining = await remaining_product_slots(db, current_user.tenant_id, tenant.is_paid)
    if remaining is not None and remaining <= 0:
        raise HTTPException(
            status_code=403,
            detail=f"Plan freemium limité à {FREEMIUM_MAX_PRODUCTS} produits. Passez à un plan payant pour en ajouter davantage.",
        )

    repo = ProductRepository(db)
    existing = await repo.get_by_sku(current_user.tenant_id, payload.sku)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Un produit avec le SKU '{payload.sku}' existe déjà")

    product = Product(tenant_id=current_user.tenant_id, **payload.model_dump())
    await repo.add(product)
    await db.commit()
    await db.refresh(product)
    return product


@router.get("", response_model=list[ProductResponse])
async def search_products(
    query: str | None = None,
    category_id: UUID | None = None,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Section 14 — recherche produit. Cette route est la base directe du futur outil IA
    `search_products` : mêmes paramètres, même garantie d'isolation par tenant (section 30).
    """
    repo = ProductRepository(db)
    return await repo.search(
        tenant_id=current_user.tenant_id,
        query=query,
        category_id=category_id,
        min_price=float(min_price) if min_price is not None else None,
        max_price=float(max_price) if max_price is not None else None,
    )


@router.get("/export/xlsx")
async def export_products_xlsx(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Export du catalogue en Excel — DOIT rester déclaré avant /{product_id} : sinon
    FastAPI tente de parser "export" comme un UUID et échoue avant même d'essayer cette route.
    """
    import io

    from fastapi.responses import StreamingResponse
    from openpyxl import Workbook

    repo = ProductRepository(db)
    products = await repo.search(tenant_id=current_user.tenant_id, query=None, limit=10000)

    wb = Workbook()
    ws = wb.active
    ws.title = "Catalogue"
    ws.append(["SKU", "Nom", "Description", "Prix", "Devise", "Stock", "Image URL", "Actif"])
    for p in products:
        ws.append([p.sku, p.name, p.description or "", float(p.price), p.currency, p.stock_quantity, p.image_url or "", "Oui" if p.active else "Non"])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=catalogue-bob.xlsx"},
    )


@router.get("/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = ProductRepository(db)
    product = await repo.get(tenant_id=current_user.tenant_id, record_id=product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    return product


@router.put("/{product_id}", response_model=ProductResponse, dependencies=[Depends(require_role("MANAGER"))])
async def update_product(
    product_id: UUID,
    payload: ProductUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = ProductRepository(db)
    product = await repo.get(tenant_id=current_user.tenant_id, record_id=product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Produit introuvable")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(product, field, value)

    await db.commit()
    await db.refresh(product)
    return product


@router.delete("/{product_id}", status_code=204, dependencies=[Depends(require_role("MANAGER"))])
async def delete_product(
    product_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Suppression DOUCE (active=False), jamais une vraie suppression en base : un produit
    déjà vendu est référencé par des commandes existantes (contrainte de clé étrangère),
    et l'historique des ventes ne doit jamais pouvoir se casser.
    """
    repo = ProductRepository(db)
    product = await repo.get(tenant_id=current_user.tenant_id, record_id=product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    product.active = False
    await db.commit()


@router.post("/{product_id}/image", response_model=ProductResponse, dependencies=[Depends(require_role("MANAGER"))])
async def upload_product_image(
    product_id: UUID,
    file: UploadFile,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stocke le fichier sur disque (monté en volume, persiste aux redéploiements) et met à jour image_url."""
    import uuid as uuid_module
    from pathlib import Path

    from app.core.config import get_settings

    repo = ProductRepository(db)
    product = await repo.get(tenant_id=current_user.tenant_id, record_id=product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Produit introuvable")

    allowed_ext = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    ext = (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
    if ext not in allowed_ext:
        raise HTTPException(status_code=400, detail="Format d'image non supporté (jpg, png, webp uniquement)")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image trop volumineuse (max 5 Mo)")

    upload_dir = Path(__file__).resolve().parents[2] / "static" / "uploads" / "products"
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid_module.uuid4().hex}.{ext}"
    (upload_dir / filename).write_bytes(content)

    settings = get_settings()
    product.image_url = f"{settings.public_base_url}/uploads/products/{filename}"

    await db.commit()
    await db.refresh(product)
    return product
