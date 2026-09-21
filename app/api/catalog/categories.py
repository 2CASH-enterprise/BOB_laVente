from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.repositories.category_repository import CategoryRepository
from app.schemas.catalog import CategoryCreate, CategoryResponse

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])


@router.post("", response_model=CategoryResponse, status_code=201, dependencies=[Depends(require_role("MANAGER"))])
async def create_category(
    payload: CategoryCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = CategoryRepository(db)
    category = await repo.get_or_create_by_name(current_user.tenant_id, payload.name)
    if payload.parent_id is not None:
        category.parent_id = payload.parent_id
    await db.commit()
    await db.refresh(category)
    return category


@router.get("", response_model=list[CategoryResponse])
async def list_categories(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = CategoryRepository(db)
    return await repo.list(tenant_id=current_user.tenant_id, limit=200)


@router.get("/{category_id}", response_model=CategoryResponse)
async def get_category(
    category_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = CategoryRepository(db)
    category = await repo.get(tenant_id=current_user.tenant_id, record_id=category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Catégorie introuvable")
    return category
