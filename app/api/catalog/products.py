from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
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
