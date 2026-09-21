from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.knowledge_entry import KnowledgeEntry
from app.repositories.knowledge_entry_repository import KnowledgeEntryRepository
from app.schemas.knowledge import KnowledgeEntryCreate, KnowledgeEntryResponse, KnowledgeEntryUpdate
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


@router.get("", response_model=list[KnowledgeEntryResponse])
async def list_knowledge_entries(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = KnowledgeEntryRepository(db)
    return await repo.list(tenant_id=current_user.tenant_id, limit=200)


@router.post("", response_model=KnowledgeEntryResponse, status_code=201, dependencies=[Depends(require_role("MANAGER"))])
async def create_knowledge_entry(
    payload: KnowledgeEntryCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entry = KnowledgeEntry(tenant_id=current_user.tenant_id, **payload.model_dump())
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


@router.put("/{entry_id}", response_model=KnowledgeEntryResponse, dependencies=[Depends(require_role("MANAGER"))])
async def update_knowledge_entry(
    entry_id: UUID,
    payload: KnowledgeEntryUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = KnowledgeEntryRepository(db)
    entry = await repo.get(tenant_id=current_user.tenant_id, record_id=entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Entrée introuvable")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(entry, field, value)

    await db.commit()
    await db.refresh(entry)
    return entry


@router.delete("/{entry_id}", status_code=204, dependencies=[Depends(require_role("MANAGER"))])
async def delete_knowledge_entry(
    entry_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = KnowledgeEntryRepository(db)
    entry = await repo.get(tenant_id=current_user.tenant_id, record_id=entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Entrée introuvable")
    await db.delete(entry)
    await db.commit()
