from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeEntryCreate(BaseModel):
    category: str = Field(default="AUTRE")
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    active: bool = True


class KnowledgeEntryUpdate(BaseModel):
    category: str | None = None
    title: str | None = None
    content: str | None = None
    active: bool | None = None


class KnowledgeEntryResponse(BaseModel):
    id: UUID
    category: str
    title: str
    content: str
    active: bool
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
