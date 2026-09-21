from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.product import Product
from app.models.product_complement import ProductComplement
from app.repositories.product_repository import ProductRepository
from app.schemas.product_complement import ProductComplementCreate, ProductComplementResponse

router = APIRouter(prefix="/api/v1/products", tags=["products"])


@router.get("/{product_id}/complements", response_model=list[ProductComplementResponse])
async def list_complements(
    product_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(ProductComplement, Product)
        .join(Product, Product.id == ProductComplement.complement_product_id)
        .where(ProductComplement.tenant_id == current_user.tenant_id, ProductComplement.product_id == product_id)
    )
    rows = (await db.execute(stmt)).all()
    return [
        ProductComplementResponse(
            id=link.id,
            product_id=link.product_id,
            complement_product_id=link.complement_product_id,
            complement_name=product.name,
            complement_price=float(product.price),
            complement_currency=product.currency,
        )
        for link, product in rows
    ]


@router.post(
    "/{product_id}/complements",
    response_model=ProductComplementResponse,
    status_code=201,
    dependencies=[Depends(require_role("MANAGER"))],
)
async def add_complement(
    product_id: UUID,
    payload: ProductComplementCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if product_id == payload.complement_product_id:
        raise HTTPException(status_code=400, detail="Un produit ne peut pas être son propre complémentaire")

    product_repo = ProductRepository(db)
    main = await product_repo.get(tenant_id=current_user.tenant_id, record_id=product_id)
    complement = await product_repo.get(tenant_id=current_user.tenant_id, record_id=payload.complement_product_id)
    if main is None or complement is None:
        raise HTTPException(status_code=404, detail="Produit introuvable")

    existing_stmt = select(ProductComplement).where(
        ProductComplement.tenant_id == current_user.tenant_id,
        ProductComplement.product_id == product_id,
        ProductComplement.complement_product_id == payload.complement_product_id,
    )
    if (await db.execute(existing_stmt)).scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Cette liaison existe déjà")

    link = ProductComplement(
        tenant_id=current_user.tenant_id, product_id=product_id, complement_product_id=payload.complement_product_id
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)

    return ProductComplementResponse(
        id=link.id,
        product_id=link.product_id,
        complement_product_id=link.complement_product_id,
        complement_name=complement.name,
        complement_price=float(complement.price),
        complement_currency=complement.currency,
    )


@router.delete("/complements/{link_id}", status_code=204, dependencies=[Depends(require_role("MANAGER"))])
async def delete_complement(
    link_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ProductComplement).where(
        ProductComplement.tenant_id == current_user.tenant_id, ProductComplement.id == link_id
    )
    link = (await db.execute(stmt)).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="Liaison introuvable")
    await db.delete(link)
    await db.commit()
